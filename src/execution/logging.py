# noinspection PyBroadException
import abc
import logging
import os
import re
from string import Template

from execution.execution_service import ExecutionService
from utils import file_utils, audit_utils
from utils.audit_utils import get_audit_name
from utils.collection_utils import get_first_existing
from utils.date_utils import get_current_millis, ms_to_datetime

ENCODING = 'utf8'

OUTPUT_STARTED_MARKER = '>>>>>  OUTPUT STARTED <<<<<'

LOGGER = logging.getLogger('script_server.execution.logging')


class ScriptOutputLogger:
    def __init__(self, log_file_path, output_stream, on_close_callback=None):
        self.opened = False
        self.output_stream = output_stream

        self.log_file_path = log_file_path
        self.log_file = None
        self.on_close_callback = on_close_callback

    def start(self):
        self._ensure_file_open()

        self.output_stream.subscribe(self)

    def _ensure_file_open(self):
        if self.opened:
            return

        try:
            self.log_file = open(self.log_file_path, 'wb')
        except:
            LOGGER.exception("Couldn't create a log file")

        self.opened = True

    def __log(self, text):
        if not self.opened:
            LOGGER.exception('Attempt to write to not opened logger')
            return

        if not self.log_file:
            return

        try:
            if text is not None:
                self.log_file.write(text.encode(ENCODING))
                self.log_file.flush()
        except:
            LOGGER.exception("Couldn't write to the log file")

    def _close(self):
        try:
            if self.log_file:
                self.log_file.close()
        except:
            LOGGER.exception("Couldn't close the log file")

        if self.on_close_callback is not None:
            self.on_close_callback()

    def on_next(self, output):
        self.__log(output)

    def on_close(self):
        self._close()

    def write_line(self, text):
        self._ensure_file_open()

        self.__log(text + os.linesep)


class HistoryEntry:
    def __init__(self):
        self.user_name = None
        self.user_id = None
        self.start_time = None
        self.script_name = None
        self.command = None
        self.id = None
        self.exit_code = None


class PostExecutionInfoProvider(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def get_exit_code(self, execution_id):
        pass


class ExecutionLoggingService:
    def __init__(self, output_folder, log_name_creator):
        self._output_folder = output_folder
        self._log_name_creator = log_name_creator

        self._visited_files = set()
        self._ids_to_file_map = {}

        file_utils.prepare_folder(output_folder)

        self._renew_files_cache()

    def start_logging(self, execution_id,
                      user_name,
                      user_id,
                      script_name,
                      command,
                      output_stream,
                      post_execution_info_provider,
                      all_audit_names,
                      start_time_millis=None):

        if start_time_millis is None:
            start_time_millis = get_current_millis()

        log_filename = self._log_name_creator.create_filename(
            execution_id, all_audit_names, script_name, start_time_millis)
        log_file_path = os.path.join(self._output_folder, log_filename)
        log_file_path = file_utils.create_unique_filename(log_file_path)

        def write_post_execution_info():
            self._write_post_execution_info(execution_id, log_file_path, post_execution_info_provider)

        output_logger = ScriptOutputLogger(log_file_path, output_stream, write_post_execution_info)
        output_logger.write_line('id:' + execution_id)
        output_logger.write_line('user_name:' + user_name)
        output_logger.write_line('user_id:' + user_id)
        output_logger.write_line('script:' + script_name)
        output_logger.write_line('start_time:' + str(start_time_millis))
        output_logger.write_line('command:' + command)
        output_logger.write_line(OUTPUT_STARTED_MARKER)
        output_logger.start()

        log_filename = os.path.basename(log_file_path)
        self._visited_files.add(log_filename)
        self._ids_to_file_map[execution_id] = log_filename

    def get_history_entries(self):
        self._renew_files_cache()

        result = []

        for file in self._ids_to_file_map.values():
            history_entry = self._extract_history_entry(file)
            if history_entry is not None:
                result.append(history_entry)

        return result

    def find_history_entry(self, execution_id):
        self._renew_files_cache()

        file = self._ids_to_file_map.get(execution_id)
        if file is None:
            LOGGER.warning('find_history_entry: file for %s id not found', execution_id)
            return None

        entry = self._extract_history_entry(file)
        if entry is None:
            LOGGER.warning('find_history_entry: cannot parse file for %s', execution_id)

        return entry

    def find_log(self, execution_id):
        self._renew_files_cache()

        file = self._ids_to_file_map.get(execution_id)
        if file is None:
            LOGGER.warning('find_log: file for %s id not found', execution_id)
            return None

        file_content = file_utils.read_file(os.path.join(self._output_folder, file),
                                            keep_newlines=True)
        log = file_content.split(OUTPUT_STARTED_MARKER, 1)[1]
        return _lstrip_any_linesep(log)

    def _extract_history_entry(self, file):
        file_path = os.path.join(self._output_folder, file)
        correct_format, parameters_text = self._read_parameters_text(file_path)
        if not correct_format:
            return None
        parameters = self._parse_history_parameters(parameters_text)
        return self._parameters_to_entry(parameters)

    @staticmethod
    def _read_parameters_text(file_path):
        parameters_text = ''
        correct_format = False
        with open(file_path, 'r', encoding=ENCODING) as f:
            for line in f:
                if _rstrip_once(line, '\n') == OUTPUT_STARTED_MARKER:
                    correct_format = True
                    break
                parameters_text += line
        return correct_format, parameters_text

    def _renew_files_cache(self):
        cache = self._ids_to_file_map

        obsolete_ids = []
        for id, file in cache.items():
            path = os.path.join(self._output_folder, file)
            if not os.path.exists(path):
                obsolete_ids.append(id)

        for obsolete_id in obsolete_ids:
            LOGGER.info('Logs for execution #' + obsolete_id + ' were deleted')
            del cache[obsolete_id]

        for file in os.listdir(self._output_folder):
            if not file.lower().endswith('.log'):
                continue

            if file in self._visited_files:
                continue

            self._visited_files.add(file)

            entry = self._extract_history_entry(file)
            if entry is None:
                continue

            cache[entry.id] = file

    @staticmethod
    def _create_log_identifier(audit_name, script_name, start_time):
        audit_name = file_utils.to_filename(audit_name)

        date_string = ms_to_datetime(start_time).strftime("%y%m%d_%H%M%S")

        script_name = script_name.replace(" ", "_")
        log_identifier = script_name + "_" + audit_name + "_" + date_string
        return log_identifier

    @staticmethod
    def _parse_history_parameters(parameters_text):
        current_value = None
        current_key = None

        parameters = {}
        for line in parameters_text.splitlines(keepends=True):
            match = re.fullmatch('([\w_]+):(.*\r?\n)', line)
            if not match:
                current_value += line
                continue

            if current_key is not None:
                parameters[current_key] = _rstrip_once(current_value, '\n')

            current_key = match.group(1)
            current_value = match.group(2)

        if current_key is not None:
            parameters[current_key] = _rstrip_once(current_value, '\n')

        return parameters

    @staticmethod
    def _parameters_to_entry(parameters):
        id = parameters.get('id')
        if not id:
            return None

        entry = HistoryEntry()
        entry.id = id
        entry.script_name = parameters.get('script')
        entry.user_name = parameters.get('user_name')
        entry.user_id = parameters.get('user_id')
        entry.command = parameters.get('command')

        exit_code = parameters.get('exit_code')
        if exit_code is not None:
            entry.exit_code = int(exit_code)

        start_time = parameters.get('start_time')
        if start_time:
            entry.start_time = ms_to_datetime(int(start_time))

        return entry

    @staticmethod
    def _write_post_execution_info(execution_id, log_file_path, post_execution_info_provider):
        exit_code = post_execution_info_provider.get_exit_code(execution_id)
        if exit_code is None:
            return

        file_content = file_utils.read_file(log_file_path, keep_newlines=True)

        file_parts = file_content.split(OUTPUT_STARTED_MARKER + os.linesep, 1)
        parameters_text = file_parts[0]
        parameters_text += 'exit_code:' + str(exit_code) + os.linesep

        new_content = parameters_text + OUTPUT_STARTED_MARKER + os.linesep + file_parts[1]
        file_utils.write_file(log_file_path, new_content.encode(ENCODING), byte_content=True)


class LogNameCreator:
    def __init__(self, filename_pattern=None, date_format=None) -> None:
        self._date_format = date_format if date_format else '%y%m%d_%H%M%S'
        if not filename_pattern:
            filename_pattern = '${SCRIPT}_${AUDIT_NAME}_${DATE}'
        self._filename_template = Template(filename_pattern)

    def create_filename(self, execution_id, all_audit_names, script_name, start_time):
        audit_name = get_audit_name(all_audit_names)
        audit_name = file_utils.to_filename(audit_name)

        date_string = ms_to_datetime(start_time).strftime(self._date_format)

        username = get_first_existing(all_audit_names, audit_utils.AUTH_USERNAME, audit_utils.PROXIED_USERNAME)

        mapping = {
            'ID': execution_id,
            'USERNAME': username,
            'HOSTNAME': get_first_existing(all_audit_names, audit_utils.PROXIED_HOSTNAME, audit_utils.HOSTNAME,
                                           default='unknown-host'),
            'IP': get_first_existing(all_audit_names, audit_utils.PROXIED_IP, audit_utils.IP),
            'DATE': date_string,
            'AUDIT_NAME': audit_name,
            'SCRIPT': script_name
        }

        filename = self._filename_template.safe_substitute(mapping)
        if not filename.lower().endswith('.log'):
            filename += '.log'

        filename = filename.replace(" ", "_")

        return filename


class ExecutionLoggingInitiator:
    def __init__(self, execution_service: ExecutionService, execution_logging_service):
        self._execution_logging_service = execution_logging_service
        self._execution_service = execution_service

    def start(self):
        execution_service = self._execution_service
        logging_service = self._execution_logging_service

        def started(execution_id):
            script_config = execution_service.get_config(execution_id)
            script_name = str(script_config.name)
            audit_name = execution_service.get_audit_name(execution_id)
            owner = execution_service.get_owner(execution_id)
            all_audit_names = execution_service.get_all_audit_names(execution_id)
            output_stream = execution_service.get_anonymized_output_stream(execution_id)
            audit_command = execution_service.get_audit_command(execution_id)

            logging_service.start_logging(
                execution_id,
                audit_name,
                owner,
                script_name,
                audit_command,
                output_stream,
                execution_service,
                all_audit_names)

        self._execution_service.add_start_listener(started)


def _rstrip_once(text, char):
    if text.endswith(char):
        text = text[:-1]

    return text


def _lstrip_any_linesep(text):
    if text.startswith('\r\n'):
        return text[2:]

    if text.startswith(os.linesep):
        return text[len(os.linesep):]

    return text
