import copy
import os
import re
from pathlib import Path
from typing import List, Union

import yara
from yara import WarningError as YaraWarningError
from yara import TimeoutError as YaraTimeoutError

from handlers.log_handler import create_logger
from yara_toolkit.utils import sanitize_identifier
from yara_toolkit.yara_meta import YaraMeta
from yara_toolkit.yara_string import YaraString
from yara_toolkit.keywords import KEYWORDS
from handlers.config_handler import CONFIG

log = create_logger(__name__)

INVALID_IDENTIFIERS = [].extend(KEYWORDS)  # FIXME: Implement validity check against reserved kw.

YARA_VAR_SYMBOL = "$"
CONDITION_INDENT_LENGTH = 8
SOURCE_FILE_EXTENSION = ".yar"
COMPILED_FILE_EXTENSION = ".bin"
RULES_DIR = os.path.join(CONFIG["theoracle_local_path"], CONFIG["theoracle_repo_rules_dir"])


class YaraRuleSyntaxError(Exception):
    def __init__(self, message: Union[str, None], yara_syntax_error_exc: yara.SyntaxError = None, rule=None, line_number=None,
                 column_number=None, column_range=None, word=None):
        super().__init__(message)

        if message is None:
            if yara_syntax_error_exc is None:
                self.message = "Column number: {column_number} (columns: " \
                    "{column_number}-{column_range}, word: '{word}')".format(
                        column_number=column_number,
                        column_range=column_range,
                        word=word)
            else:
                # Parse syntax error reason out of the SyntaxError message.
                log.debug(str(yara_syntax_error_exc))
                log.debug(str(yara_syntax_error_exc).split(':'))
                self.reason = str(yara_syntax_error_exc).split(':')[1][1:]
                log.debug(self.reason)

                self.message = "{reason} in string '{word}', columns: " \
                               "{column_number}-{column_range}.".format(
                                reason=self.reason,
                                column_number=column_number,
                                column_range=column_range,
                                word=word)
                log.debug(self.message)
        else:
            self.message = message

        self.rule = rule
        self.line_number = line_number
        self.column_number = column_number
        self.column_range = column_range
        self.word = word

    def __str__(self):
        return self.message


class YaraMatchCallback:
    """
    Class to use with yara.Rules.match in order to avoid messy globals that has issues if more than one
    YaraRule uses it at the same time.

    Usage: Initialise it, then pass the callback function reference to yara.Rules.match(callback=...)

    Official documentation: https://yara.readthedocs.io/en/latest/yarapython.html
    """
    def __init__(self):
        self.log = create_logger(__name__)

        self.matches = None
        self.rule = None
        self.namespace = None
        self.tags = None
        self.meta = None
        self.strings = None

    def callback(self, callback_dict: dict):
        """
        Function to be passed to yara.Rules.match.

        :param callback_dict:   The passed dictionary will be something like this:
                                    {
                                      'tags': ['foo', 'bar'],
                                      'matches': True,
                                      'namespace': 'default',
                                      'rule': 'my_rule',
                                      'meta': {},
                                      'strings': [(81L, '$a', 'abc'), (141L, '$b', 'def')]
                                    }

                                The matches field indicates if the rule matches the data or not.
                                The strings fields is a list of matching strings, with vectors of the form:
                                    (<offset>, <string identifier>, <string data>)



        :return yara.CALLBACK_ABORT:    Stop after the first rule, as we only have one.
        """
        self.log.info("YaraMatchCallback.callback({})".format(callback_dict))

        if "matches" in callback_dict:
            self.matches = callback_dict["matches"]
        if "rule" in callback_dict:
            self.rule = callback_dict["rule"]
        if "namespace" in callback_dict:
            self.namespace = callback_dict["namespace"]
        if "tags" in callback_dict:
            self.tags = callback_dict["tags"]
        if "meta" in callback_dict:
            self.meta = callback_dict["meta"]
        if "strings" in callback_dict:
            self.strings = callback_dict["strings"]

        # Stop applying rules to your data.
        return yara.CALLBACK_ABORT


class YaraRule:
    def __init__(self, name: str, tags: List[str] = None, meta: List[YaraMeta] = None,
                 strings: List[YaraString] = None, condition: str = None, namespace: str = None,
                 compiled_blob: yara.Rules = None, compiled_path: str = None,
                 compiled_match_source: bool = None):
        """
        YARA rule object.

        :param name:            Rule name.
        :param tags:            List of tags.
        :param meta:            Metadata.
        :param strings:         List of strings (variables).
        :param condition:       Condition string.
        :param namespace:       Namespace of YARA rule.
        :param compiled_blob:   Raw yara.Rules object generated by yara.compile
                                (usually set when spawned by cls from_compiled_file).
        :param compiled_path:   Path to the compiled YARA rule
                                (usually set when spawned by cls from_compiled_file).
        """
        self.log = create_logger(__name__)

        self.name: str = sanitize_identifier(name)

        if tags is not None:
            self.tags: list = [sanitize_identifier(x) for x in tags]

        self.meta: List[YaraMeta] = meta
        self.strings: List[YaraString] = strings

        if condition is not None:
            # Sanitize every identifier in the condition string.
            if len(condition) > 0:
                self.condition = \
                    " ".join([part[0] + sanitize_identifier(part[1:])
                              if part[0] == YARA_VAR_SYMBOL else part for part in condition.split(' ')])
            else:
                self.condition = condition

        self.namespace = namespace
        self.compiled_blob = compiled_blob
        self.compiled_path = compiled_path
        self.compiled_match_source = compiled_match_source

    @classmethod
    def from_dict(cls, dct: dict):
        """
        Initialize YaraRule from a dict.

        :param dct: Dict on the form of:
                    {
                        name: str,
                        tags: List[str],
                        meta: {identifier, value, value_type},
                        strings: [{identifier, value, value_type, string_type, modifiers, modifier_str, str}]
                        condition: str
                    }.
        :return:
        """
        return cls(name=dct["name"],
                   tags=dct["tags"],
                   meta=[YaraMeta(ym["identifier"], ym["value"], ym["value_type"]) for ym in dct["meta"]],
                   strings=
                   [YaraString(ys["identifier"], ys["value"], ys["value_type"], ys["string_type"], ys["modifiers"])
                    for ys in dct["strings"]],
                   condition=dct["condition"])

    @classmethod
    def from_source_file(cls, source_path=None):
        """Initialize YaraRule from sourcecode using own custom written parser."""
        try:
            source_code = None
            with open(source_path, 'r') as f:
                source_code = f.read()

            log.debug(source_code)

            constructor_line_pattern = re.compile(
                r"(?P<rule_keyword>rule)\s+(?P<rule_identifier>\w+)(?P<tag_body>(?P<tag_delimiter>:)\s+(?P<tags>[\s+\w]+))?\{(?P<rule_body>.*)\}",
                re.MULTILINE | re.DOTALL)

            constructor_line_match = constructor_line_pattern.search(source_code)

            rule_pattern = re.compile(
                r"(?P<rule_keyword>rule)\s+(?P<rule_identifier>\w+)"
                r"(?P<tag_body>(?P<tag_delimiter>:)\s+(?P<tags>[\s+\w]+))?"
                r"\{(?P<body>.*(?P<meta_body>(?P<meta_constructor>meta:)\s+(?P<meta_content>.*\w))?\s+"
                r"(?P<strings_body>(?P<strings_constructor>strings:)\s+(?P<strings_content>.*[\w\}\)]))?.*)"
                r"(?P<condition_body>(?P<condition_constructor>condition:)\s+(?P<condition_content>.*)).*\}",
                re.MULTILINE | re.DOTALL
            )

            rule_match = constructor_line_pattern.search(source_code)

            log.debug(rule_match.groupdict())

            name = rule_match.groupdict()["rule_identifier"]
            tags = rule_match.groupdict()["tags"]
            # condition = rule_match.groupdict()["condition_content"]
            condition = None

            body = rule_match.groupdict()["rule_body"]

            log.debug("body:\n{}".format(body))

            ####### Seek thru the whole shebang until you match keyword.
            # Split on whitespace to eliminate it as a factor.
            body_items = []
            for item in body.split(' '):
                if item != '':
                    body_items.append(item)

            log.info(body_items)

            # Create a copy of body to break down in order to find the true meta and string keywords
            modified_body = copy.deepcopy(body)
            log.info("modified body:\n{}".format(modified_body))
            # modified_body = re.sub(r"\".*\"")

            # Make a pass to replace all string values with placeholders.
            inside_quoted_string = False
            inside_regex_string = False
            inside_hex_string = False
            inside_escape_sequence = False
            inside_multichar_escape_sequence = False
            inside_comment_line = False
            comment_line = ""
            comment_lines = []
            inside_comment_block = False
            comment_block = ""
            comment_blocks = []
            string_safe_body = ""
            escape_terminators = ['\\', '"', 't', 'n']
            escape_chars_not_to_replace = ['\n', '\t', '\r', '\b', '\f']
            chars_not_to_replace = escape_chars_not_to_replace
            chars_not_to_replace.extend(' ')
            separators = [' ', '\n', '\t']

            def is_hex_esc_sequence(s):
                """Takes a string 's' and determines if it is a hex escape sequence."""
                p = re.compile(r"^\\x[0-9][0-9]$")
                m = p.match(s)
                if m:
                    return True
                else:
                    return False

            last_line_start_index = 0
            line = ""
            for i in range(len(modified_body)):
                c = modified_body[i]  # Helps on readability.
                line += c
                if c == '\n':
                    log.info("line: {}".format(modified_body[last_line_start_index:i]))
                    last_line_start_index = i+1
                    line = ""

                if inside_quoted_string:
                    if inside_escape_sequence:
                        if inside_multichar_escape_sequence:
                            if is_hex_esc_sequence(modified_body[i-3:i+1]):
                                inside_escape_sequence = False
                                inside_multichar_escape_sequence = False
                        else:
                            if c in escape_terminators:
                                inside_escape_sequence = False
                            else:
                                # If the char after \ isn't a terminator, then this is a hex/multichar escape sequence.
                                inside_multichar_escape_sequence = True
                    else:
                        if c == '\\':
                            inside_escape_sequence = True
                        elif c == '"':
                            inside_quoted_string = False

                    # Replace current char with safe placeholder.
                    string_safe_body += '#'
                elif inside_regex_string:
                    if c == '/' and modified_body[i+1] in separators:
                        inside_regex_string = False

                    # Replace current char with safe placeholder.
                    string_safe_body += '~'
                elif inside_hex_string:
                    if c == '}' and modified_body[i + 1] in separators:
                        inside_hex_string = False

                    # Replace current char with safe placeholder.
                    string_safe_body += '¤'
                elif inside_comment_line:
                    comment_line += c

                    if c == '\n':
                        string_safe_body += c
                        log.info("comment line: {}".format(comment_line))
                        comment_lines.append(comment_line)
                        comment_line = ""
                        inside_comment_line = False
                    else:
                        string_safe_body += '@'
                elif inside_comment_block:
                    comment_block += c

                    if c == '/' and modified_body[i-1] == '*':
                        string_safe_body += '%'
                        inside_comment_block = False
                    else:
                        log.info("comment block: {}".format(comment_block))
                        comment_blocks.append(comment_block)
                        comment_block = ""
                        string_safe_body += '%' if c not in chars_not_to_replace else c
                else:
                    if c == '"':
                        inside_quoted_string = True
                        string_safe_body += '#'
                    elif c == '/' and modified_body[i+1] != '/' and modified_body[i+1] != '*':
                        inside_regex_string = True
                    elif c == '{':
                        inside_hex_string = True
                    elif c == '/' and modified_body[i+1] == '/':
                        inside_comment_line = True
                        comment_line += c
                        string_safe_body += '@'
                    elif c == '/' and modified_body[i+1] == '*':
                        inside_comment_block = True
                        string_safe_body += '%'
                    else:
                        string_safe_body += c

            log.info(string_safe_body)

            # Make a second pass with a pattern that doesn't use dotall, in order to better parse each sub-body,
            meta = None
            strings = None

            log.debug("name={}, tags={}, meta={}, strings={}, condition={}".format(name, tags, meta, strings, condition))

            return None

            return cls(name, tags, meta, strings, condition)

        except Exception as exc:
            log.exception("YaraRule.from_source_file exc", exc_info=exc)
            return None

    @classmethod
    def from_source_file_yara_python(cls, source_path=None):
        """Initialize YaraRule from sourcecode using the limited yara-python API."""
        try:
            # Compile the YARA source code (only way to get yara-python to parse the thing)
            yar_compiled = yara.compile(filepath=source_path)

            # Get the parsed source code via yara.Rules.match
            yar_src = yar_compiled.match(filepath=source_path)[0]

            name = yar_src.rule
            namespace = yar_src.namespace
            tags = yar_src.tags
            meta = [YaraMeta(identifier, value) for identifier, value in yar_src.meta.items()]
            strings = [YaraString(identifier, value.decode('utf-8')) for offset, identifier, value in yar_src.strings]

            # Get condition from the sourcecode file by hand due to it not being part of yara.Rules.
            condition = None
            this_is_the_condition = False
            with open(source_path, 'r') as f:
                for line in f.readlines():
                    if this_is_the_condition:
                        # Strip leading whitespace/indent.
                        for i in range(len(line)):
                            if line[i] == ' ':
                                continue
                            else:
                                condition = line[i:].strip('\n')
                                break
                        break

                    if 'condition' in line.lower():
                        # Next line will contain the actual condition, this one just has the declaration.
                        this_is_the_condition = True

            log.debug(condition)

            return cls(name, tags, meta, strings, condition, namespace=namespace)

        except Exception as exc:
            log.exception("YaraRule.from_source_file_yara_python exc", exc_info=exc)
            return None

    @classmethod
    def from_compiled_file(cls, yara_rules: Union[yara.Rules, str],
                           source_filename=None, compiled_filepath=None,
                           condition: str = None, rules_dir=RULES_DIR, timeout=60):
        """
        Initialize YaraRule from a compiled (binary) file.

        :param timeout:             If the match function does not finish before the specified number
                                    of seconds elapsed, a TimeoutError exception is raised.
        :param compiled_filepath:
        :param source_filename:
        :param condition:
        :param yara_rules: yara.Rules object or path to a compiled yara rules .bin
        :param rules_dir:
        :return:
        """
        if condition is None:
            # It looks like compiled YARA rules don't have a condition,
            # so we have to apply it ourselves or leave it blank.
            condition = ""

        if isinstance(yara_rules, yara.Rules):
            if source_filename is None:
                raise ValueError("yara.Rules object was given, but source_filename was not set!")

            # Load rules from yara.Rules object.
            compiled_blob: yara.Rules = yara_rules
        elif isinstance(yara_rules, str):
            compiled_filepath = os.path.join(rules_dir, yara_rules + COMPILED_FILE_EXTENSION)
            # Set source filename.
            source_filename = yara_rules + SOURCE_FILE_EXTENSION

            # Load rules from file.
            compiled_blob: yara.Rules = yara.load(
                filepath=compiled_filepath)
        else:
            raise ValueError("yara_rules must be 'yara.Rules' object or 'str' filepath to a compiled yara rules .bin")

        # The match method returns a list of instances of the class Match.
        # Instances of this class have the same attributes as the dictionary passed to the callback function,
        # with the exception of 'matches' which is ONLY passed to the callback function!
        yara_match_callback = YaraMatchCallback()
        matches: yara.Match = compiled_blob.match(filepath=os.path.join(rules_dir, source_filename),
                                                  callback=yara_match_callback.callback,
                                                  timeout=timeout)

        meta = [YaraMeta(identifier, value) for identifier, value in yara_match_callback.meta.items()]
        namespace = yara_match_callback.namespace
        name = yara_match_callback.rule
        strings = [
            YaraString(identifier, value.decode('utf-8')) for offset, identifier, value in yara_match_callback.strings]
        tags = yara_match_callback.tags

        if not yara_match_callback.matches:
            log.error("Compiled YARA does *NOT* match source code!")
            # raise
        else:
            log.info("Compiled YARA matches source code.")
            match = matches[0]
            log.info("match: {}".format(match))

        if isinstance(yara_rules, yara.Rules) and compiled_filepath is None:
            log.warning("yara.Rules object was given, but compiled_filepath was not set, "
                        "assuming same name as rule name!")
            compiled_filepath = os.path.join(rules_dir, name + COMPILED_FILE_EXTENSION)

        return cls(name, tags, meta, strings, condition,
                   namespace=namespace, compiled_blob=compiled_blob,
                   compiled_path=compiled_filepath, compiled_match_source=yara_match_callback.matches)

    def get_referenced_strings(self) -> List[YaraString]:
        """
        In YARA it is a SyntaxError to have unreferenced strings/vars,
        so these need to be rinsed out before rule compilation.

        :return: Returns dict of strings that are referenced in the conditional statement.
        """
        # Find all occurrences of words starting with $ (i.e. variable names)
        r = re.compile(r'\$[\w]*\b\S+')
        matched_condition_strings = r.findall(self.condition)

        # Get rid of mismatches by making a list of items that only matches the actual strings/vars list.
        confirmed_items = []
        for matched_condition_identifier, yara_string in zip(matched_condition_strings, self.strings):
            if sanitize_identifier(matched_condition_identifier[1:]) == yara_string.identifier:
                confirmed_items.append(yara_string)

        return confirmed_items

    def condition_as_lines(self) -> str:
        """
        Takes a condition string and returns a string with each condition on a separate line.

        :return:
        """
        return self.condition.replace(' ', '\n')

    def __str__(self, condition_as_lines=False) -> str:
        """
        Generates a YARA rule on string form.
    
        example format:
            rule RuleIdentifier
            {
                meta:
                    description = ""
    
                strings:
                    $observable1 = ""
    
                condition:
                    $observable1
            }
    
        :return:
        """
        indent = 4 * " "
        identifier_line = "rule {name}".format(name=self.name)
        meta = ""
        strings = ""
        if condition_as_lines:
            condition = "{indent}condition:\n{indent}{indent}{condition}".format(
                indent=indent, condition=self.condition_as_lines())
        else:
            condition = "{indent}condition:\n{indent}{indent}{condition}".format(
                indent=indent, condition=self.condition)
    
        # Append tags to rule line, if provided.
        tags_str = ""
        if self.tags is not None:
            if len(self.tags) > 0:
                tags_str = (": " + " ".join(self.tags))
    
        # Add the meta info block, if provided.
        if self.meta is not None:
            if bool(self.meta):
                meta = "{indent}meta:".format(indent=indent)
                for ym in self.meta:
                    meta += "\n{indent}{indent}{yara_meta}".format(indent=indent, yara_meta=str(ym))
    
        # Add the strings (read: variables) block, id provided.
        if self.strings is not None:
            if bool(self.strings):
                strings = "{indent}strings:".format(indent=indent)
                for ys in self.get_referenced_strings():
                    strings += "\n{indent}{indent}{yara_string}".format(indent=indent, yara_string=str(ys))

        # Compile the entire rule block string.
        rule_string = \
            "{identifier_line}{tags_str}\n" \
            "{start}\n" \
            "{meta}\n" \
            "\n" \
            "{strings}\n" \
            "\n" \
            "{condition}\n" \
            "{end}\n".format(identifier_line=identifier_line, tags_str=tags_str,
                             meta=meta, strings=strings, condition=condition, start='{', end='}')
    
        log.debug(rule_string)
        return rule_string

    def save_source(self, filename: str = None, file_ext=SOURCE_FILE_EXTENSION, rules_dir=RULES_DIR) -> str:
        """
        Saves source (plaintext) YARA rules to file.

        :param filename:
        :param file_ext:
        :param rules_dir:
        :return: saved filepath as a Path(PurePath) object.
        """
        if filename is None:
            filename = self.name

        # If destination directory does not exist, create it.
        if not os.path.isdir(rules_dir):
            os.mkdir(rules_dir)

        filepath = Path(rules_dir).joinpath(filename + file_ext)

        # Save YARA source rule to plaintext file using regular Python standard file I/O.
        with open(filepath, 'w') as f:
            f.write(self.__str__())

        self.log.info("Save YARA rules to file: {}".format(filepath))

        return str(filepath.resolve(strict=True))

    def determine_syntax_error_column(self, yara_syntax_error_exc, line_number: int, splitline_number: int,
                                      raise_exc=True) -> dict:
        """
        Determines the column (and range) that compilation failed on,
        using whitespace line number and newline line numbers to determine the character offset to the word.

        :param yara_syntax_error_exc:
        :param raise_exc:               Raises YaraRuleSyntaxError immediately upon finish.
        :param line_number:             Line number that failed in the whitespace string.
        :param splitline_number:        Line number that failed in the newline string.
        :return:                        dict: {"column_number", "column_range", "word"}
        """
        global CONDITION_INDENT_LENGTH

        # Create a list version of the conditions newline string, for convenience.
        condition_as_lines_list = self.condition_as_lines().split('\n')

        # Get index of the errored word in the conditions list.
        errored_word_index = splitline_number - line_number

        # Figure out the distance in chars from start of condition to bad word.
        # (Indent + chars-up-to-error + 1 whitespace + 1 human-readable-indexing)
        char_offset = CONDITION_INDENT_LENGTH + len(" ".join(condition_as_lines_list[:errored_word_index])) + 1 + 1

        if raise_exc:
            raise YaraRuleSyntaxError(message=None,
                                      yara_syntax_error_exc=yara_syntax_error_exc,
                                      rule=self,
                                      line_number=line_number,
                                      column_number=str(char_offset),
                                      column_range=str(char_offset + len(condition_as_lines_list[errored_word_index])),
                                      word=condition_as_lines_list[errored_word_index])
        else:
            return {
                "column_number": str(char_offset),
                "column_range": str(char_offset + len(condition_as_lines_list[errored_word_index])),
                "word": condition_as_lines_list[errored_word_index]
            }

    def compile(self, save_file=True, error_on_warning=False, **kwargs):
        """
        Compile YARA sourcecode into a binary (blob) file.

        :param save_file:           Saves compiled (binary blob) YARA rule to file.
        :param error_on_warning:    If true warnings are treated as errors, raising an exception.
        :param kwargs:              https://yara.readthedocs.io/en/latest/yarapython.html#yara.yara.compile
        :return:
        """
        try:
            self.compiled_blob: yara.Rules = yara.compile(
                source=self.__str__(), error_on_warning=error_on_warning, **kwargs)

            if save_file:
                self.save_compiled()
        except yara.SyntaxError as e:
            # Get line number (split on colon, then split first element
            # on whitespace, then grab the last element).
            line_number = str(e).split(':')[0].split(' ')[-1]

            try:
                # Attempt to determine column no:
                # Attempt a new (failed) compilation with condition as newlined strings,
                # in order to detect which word it fails on.
                self.compiled_blob: yara.Rules = yara.compile(
                    source=self.__str__(condition_as_lines=True), error_on_warning=error_on_warning, **kwargs)

            except yara.SyntaxError as yara_condition_newlined_exc:
                log.info("Caught YARA Syntax Error with newlined condition, "
                         "now determining the column (and range) that failed, "
                         "then raising an improved Syntax Exception...", exc_info=yara_condition_newlined_exc)
                splitline_number = int(str(e).split(':')[0].split(' ')[-1])

                # Determine the column (and range) that failed,
                # using line and splitline to determine the true word offset.
                self.determine_syntax_error_column(e, int(line_number), splitline_number, raise_exc=True)

    def save_compiled(self, filename: str = None, file_ext=COMPILED_FILE_EXTENSION, rules_dir=RULES_DIR):
        """
        Saves compiled (binary blob) YARA rule to file.

        :param filename:
        :param file_ext:
        :param rules_dir:
        :return:
        """
        if filename is None:
            filename = self.name

        # If destination directory does not exist, create it.
        if not os.path.isdir(rules_dir):
            os.mkdir(rules_dir)

        filepath = os.path.join(rules_dir, filename + file_ext)

        # Save compiled YARA rule to binary file using the Yara class' builtin.
        self.compiled_blob.save(filepath)

        # Store filepath in self for later reference.
        self.compiled_path = filepath
