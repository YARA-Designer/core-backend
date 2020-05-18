import os
import re
from pathlib import Path
from typing import List, Union

import yara

from handlers.log_handler import create_logger
from handlers.yara_handler.utils import sanitize_identifier
from handlers.yara_handler.yara_meta import YaraMeta
from handlers.yara_handler.yara_string import YaraString
from handlers.yara_handler.keywords import KEYWORDS
from handlers.config_handler import CONFIG

log = create_logger(__name__)

INVALID_IDENTIFIERS = [].extend(KEYWORDS)  # FIXME: Implement validity check against reserved kw.

SOURCE_FILE_EXTENSION = ".yar"
COMPILED_FILE_EXTENSION = ".bin"
CALLBACK_DICTS: list = []
YARA_VAR_SYMBOL = "$"
CONDITION_INDENT_LENGTH = 8


class YaraRuleSyntaxError(Exception):
    def __init__(self, message: Union[str, None], rule=None, line_number=None,
                 column_number=None, column_range=None, word=None):
        super().__init__(message)

        if message is None:
            self.message = "Syntax error! -- column number: {column_number} (columns: " \
                "{column_number}-{column_range}, word: '{word}')".format(
                    column_number=column_number,
                    column_range=column_range,
                    word=word)
        else:
            self.message = message

        self.rule = rule
        self.line_number = line_number
        self.column_number = column_number
        self.column_range = column_range
        self.word = word

    def __str__(self):
        return self.message


def compiled_rules_to_sources_str_callback(d: dict):
    """
    Callback function for when invoking yara.match method.
    The provided function will be called for every rule, no matter if matching or not.

    Function should expect a single parameter of dictionary type, and should return CALLBACK_CONTINUE
    to proceed to the next rule or CALLBACK_ABORT to stop applying rules to your data.
    :param d: Likely a dict.
    :return:
    """
    global CALLBACK_DICTS

    log.info("CALLBACK: {}".format(d))
    CALLBACK_DICTS.append(d)

    # Continue/Step
    # return CALLBACK_CONTINUE to proceed to the next rule or
    # CALLBACK_ABORT to stop applying rules to your data.
    return yara.CALLBACK_CONTINUE


class YaraRule:
    def __init__(self, name: str, tags: List[str] = None, meta: List[YaraMeta] = None,
                 strings: List[YaraString] = None, condition: str = None, namespace: str = None, compiled_blob=None):
        self.log = create_logger(__name__)

        self.name: str = sanitize_identifier(name)

        if tags is not None:
            self.tags: list = [sanitize_identifier(x) for x in tags]

        self.meta: List[YaraMeta] = meta
        self.strings = strings

        if condition is not None:
            # Sanitize every identifier in the condition string.
            if len(condition) > 0:
                self.condition = \
                    " ".join([part[0] + sanitize_identifier(part[1:])
                              if part[0] == YARA_VAR_SYMBOL else part for part in condition.split(' ')])
            else:
                self.condition = condition

        self.compiled_blob = None

    @classmethod
    def from_dict(cls, dct: dict):
        """
        Initialize YaraRule from a dict.

        :param dct: Dict on the form of:
                    {
                        rule: str,
                        tags: List[str],
                        meta: {identifier: value},
                        observables: {identifier: value},
                        condition: str
                    }.
        :return:
        """
        return cls(name=dct["rule"],
                   tags=dct["tags"],
                   meta=[YaraMeta(identifier, value) for identifier, value in dct["meta"].items()],
                   strings=
                   [YaraString(identifier, value["observable"]) for identifier, value in dct["observables"].items()],
                   condition=dct["condition"])

    @classmethod
    def from_compiled_file(cls, yara_rules: Union[yara.Rules, str], condition: str = None, rules_dir=None):
        """
        Initialize YaraRule from a compiled (binary) file.

        :param condition:
        :param yara_rules: yara.Rules object or path to a compiled yara rules .bin
        :param rules_dir:
        :return:
        """
        if condition is None:
            condition = ""

        # If no custom rules dir is given, use TheOracle's.
        if rules_dir is None:
            rules_dir = os.path.join(CONFIG["theoracle_local_path"], CONFIG["theoracle_repo_rules_dir"])

        if isinstance(yara_rules, yara.Rules):
            # Load rules from yara.Rules object.
            compiled_blob: yara.Rules = yara_rules
        elif isinstance(yara_rules, str):
            # Load rules from file.
            compiled_blob: yara.Rules = yara.load(
                filepath=os.path.join(rules_dir, yara_rules + COMPILED_FILE_EXTENSION))
        else:
            raise ValueError("yara_rules must be 'yara.Rules' object or 'str' filepath to a compiled yara rules .bin")

        # The match method returns a list of instances of the class Match.
        # Instances of this class have the same attributes as the dictionary passed to the callback function.
        matches: yara.Match = compiled_blob.match(filepath=os.path.join(rules_dir, yara_rules + SOURCE_FILE_EXTENSION),  # FIXME: 'yara_rules' will fail for yara.Rules which is not a str!
                                                   callback=compiled_rules_to_sources_str_callback)

        # Copy Matches attributes and misc over to a more malleable dict.
        # match = match_to_dict(matches[0], condition=condition, matches=CALLBACK_DICTS[0]["matches"])

        relevant_match = matches[0]

        # Returned values from yara.Match.match() is a list of Match objects on the form of:
        # Match.meta: dict
        meta = [YaraMeta(identifier, value) for identifier, value in relevant_match.meta.items()]
        # Match.namespace: str
        namespace = relevant_match.namespace
        # Match.rule: str
        name = relevant_match.rule
        # Match.strings: List[Tuples]:
        #   Tuple: (some_int: int, identifier: str, data: binary encoded str)
        strings = \
            [YaraString(identifier, value.decode('utf-8')) for some_int, identifier, value in relevant_match.strings]
        # Match.tags: list
        tags = relevant_match.tags

        if condition is None:
            # It looks like compiled YARA rules don't have a condition,
            # so we have to apply it ourselves or leave it blank.
            condition = ""

        global CALLBACK_DICTS
        rule_is_a_match = CALLBACK_DICTS[0]["matches"]  # FIXME: Unused.

        # Reset the global callback data list.
        CALLBACK_DICTS = []

        log.info("match: {}".format(relevant_match))

        log.debug("compiled_rules_to_source_strings matches: {}".format(rule_is_a_match))  # FIXME: Debug

        return cls(name, tags, meta, strings, condition, namespace=namespace, compiled_blob=compiled_blob)

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

    def condition_as_lines(self):
        """
        Takes a condition string and returns a string with each condition on a separate line.

        :return:
        """
        return self.condition.replace(' ', '\n')

    def __str__(self):
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
        condition = "{indent}condition:\n{indent}{indent}{condition}".format(indent=indent, condition=self.condition)
    
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
    
        return rule_string

    def save_source(self, filename: str = None, file_ext=SOURCE_FILE_EXTENSION, rules_dir=None):
        """
        Saves source (plaintext) YARA rules to file.

        :param filename:
        :param file_ext:
        :param rules_dir:
        :return: saved filepath as a Path(PurePath) object.
        """
        if filename is None:
            filename = self.name

        # If no custom rules dir is given, use TheOracle's.
        if rules_dir is None:
            rules_dir = os.path.join(CONFIG["theoracle_local_path"], CONFIG["theoracle_repo_rules_dir"])

        # If destination directory does not exist, create it.
        if not os.path.isdir(rules_dir):
            os.mkdir(rules_dir)

        # filepath = Path(os.path.join(rules_dir, filename + file_ext))
        filepath = Path(rules_dir).joinpath(filename + file_ext)

        # Save YARA source rule to plaintext file using regular Python standard file I/O.
        with open(filepath, 'w') as f:
            f.write(self.__str__())

        self.log.info("Saved YARA rules to file: {}".format(filepath))
        return str(filepath.resolve(strict=True))

    def determine_syntax_error_column(self, line_number: int, splitline_number: int, raise_exc=True) -> dict:
        """
        Determines the column (and range) that compilation failed on,
        using whitespace line number and newline line numbers to determine the character offset to the word.

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

    def compile(self, save_file=True, error_on_warning=True, **kwargs):
        """
        Compile YARA sourcecode into a binary (blob) file.

        :param save_file:
        :param error_on_warning:
        :param kwargs:
        :return:
        """
        try:
            self.compiled_blob: yara.Rules = yara.compile(
                source=self.__str__(), error_on_warning=error_on_warning, **kwargs)

            if save_file:
                self.save_compiled()
        except yara.SyntaxError as e:
            # Get line number (split on colon, then split first element
            # on whitespace then grab the last element.
            line_number = str(e).split(':')[0].split(' ')[-1]

            try:
                # Attempt to determine column no:
                # Attempt a new (failed) compilation with condition as newlined strings,
                # in order to detect which word it fails on.
                self.compiled_blob: yara.Rules = yara.compile(
                    source=self.__str__(), error_on_warning=error_on_warning, **kwargs)

            except yara.SyntaxError:
                splitline_number = int(str(e).split(':')[0].split(' ')[-1])

                # Determine the column (and range) that failed,
                # using line and splitline to determine the true word offset.
                self.determine_syntax_error_column(int(line_number), splitline_number, raise_exc=True)
            #     pass
            # pass

    def save_compiled(self, filename: str = None, file_ext=COMPILED_FILE_EXTENSION, rules_dir=None):
        """
        Saves compiled (binary blob) YARA rule to file.

        :param filename:
        :param file_ext:
        :param rules_dir:
        :return:
        """
        if filename is None:
            filename = self.name

        # If no custom rules dir is given, use TheOracle's.
        if rules_dir is None:
            rules_dir = os.path.join(CONFIG["theoracle_local_path"], CONFIG["theoracle_repo_rules_dir"])

        # If destination directory does not exist, create it.
        if not os.path.isdir(rules_dir):
            os.mkdir(rules_dir)

        filepath = os.path.join(rules_dir, filename + file_ext)

        # Save compiled YARA rule to binary file using the Yara class' builtin.
        self.compiled_blob.save(filepath)