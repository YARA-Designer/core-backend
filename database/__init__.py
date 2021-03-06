from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session

# Create a database engine.
engine = create_engine('sqlite:///yara_rules.db')

# Create a configured "Session" class.
db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))

# Returns a new base class from which all mapped classes should inherit.
Base = declarative_base()


def init_db():
    # import all modules here that might define models so that
    # they will be registered properly on the metadata.  Otherwise
    # you will have to import them first before calling init_db()
    from database.models import YaraRuleDB, YaraTagDB, YaraStringDB, YaraStringModifierDB, YaraMetaDB
    Base.metadata.create_all(bind=engine)



