import json
from datetime import date

from flask import Flask
from flask.json import JSONEncoder
from flask_cors import CORS

from apis import blueprint as api
from flask_helpers import ReverseProxied
from handlers import config_handler
from handlers.log_handler import create_logger
from handlers import git_handler
from database import init_db

log = create_logger(__name__)
log_utility_functions = create_logger("{}.utility_functions".format(__name__))


class MyJSONEncoder(JSONEncoder):
    def default(self, o):
        """Override Flask JSONEncoder's date serializer (RFC 1123) to use ISO8601.."""
        if isinstance(o, date):
            return o.isoformat()

        return super().default(o)


class MyFlask(Flask):
    json_encoder = MyJSONEncoder


def utility_functions():
    def print_in_console(message):
        print(str(message))

    def print_in_log(message):
        log_utility_functions.debug(str(message))

    return dict(mdebug=print_in_console, log=print_in_log)


def filter_suppress_none(val):
    """
    A filter that prevents Jinja2 from printing "None" when executing code with no return value.

    This filter will return/print an empty string instead of None,
    so no actual extra (garbage) text gets added to the HTML.

    :param val:
    :return:
    """
    if val is not None:
        return val
    else:
        return ''


def get_flask_rule_by_name(name: str):
    for r in app.url_map.iter_rules():
        if r.rule == name:
            return r


def log_added_route(name: str, include_obj=True):
    rule = get_flask_rule_by_name(name)
    log.info("Added TheHive listener Flask App route '{rule}': "
             "view_func: {endpoint}, methods: {methods}".format(**rule.__dict__))

    if include_obj is True:
        log.debug2(rule.__dict__)


if __name__ == "__main__":
    # Get config.
    config = config_handler.load_config()
    log.info("Loaded configuration: '{}'.".format(
        config_handler.CONFIG_FILE if config_handler.has_custom_config() else 'default'))
    log.debug("CONFIG (overrides: {overrides}):\n{js}".format(
        overrides=config_handler.CONFIG_OVERRIDES,
        js=json.dumps(config_handler.CONFIG, indent=4)))

    # Initialize database.
    init_db()
    log.info("Initialized database.")

    # Set up TheOracle Git.
    git_handler.clone_if_not_exist(url=config["theoracle_repo"], path=config["theoracle_local_path"])

    # Set up Flask.
    app = MyFlask(__name__)
    # Make Flask app support reverse proxy with sub-path.
    app.wsgi_app = ReverseProxied(app.wsgi_app)
    log.info("Configured Flask app.")
    CORS(app)

    # Add utility functions like print_in_console ('mdebug' in Jinja2 code)
    app.context_processor(utility_functions)
    log.info("Added Flask app context processor utility functions: {}.".format(
        [str(func) for func in utility_functions.__call__()]))

    # Add filters.
    app.jinja_env.filters['ignore_none'] = filter_suppress_none
    log.info("Added Flask app Jinja2 filters: ['ignore_none'].")

    # Set up Flask-RESTX (API).
    app.register_blueprint(api, url_prefix='/api/v1')

    # Run the Flask Webserver.
    log.info("Starting Flask App Webserver, listening on: {host}:{port}".format(
        host=config["listener_bind_host"], port=config["listener_bind_port"]))
    app.run(host=config["listener_bind_host"], port=config["listener_bind_port"], debug=True)

