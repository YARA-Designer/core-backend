from flask import Blueprint
from flask_restx import Api
from .core import api as ns_core
from .thehive import api as ns_thehive
from .theoracle import api as ns_theoracle

blueprint = Blueprint('api', __name__)

api = Api(blueprint,
          title='YARA-Designer Core APIs',
          version='1.0',
          description='A description',  # FIXME: Add an API description.
          # All API metadata
          )

api.add_namespace(ns_core, path='/core')
api.add_namespace(ns_thehive, path='/thehive')
api.add_namespace(ns_theoracle, path='/theoracle')
