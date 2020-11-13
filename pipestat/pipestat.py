import psycopg2
from psycopg2 import sql
from psycopg2.extras import DictCursor, Json
from psycopg2.extensions import connection

from logging import getLogger
from contextlib import contextmanager
from collections.abc import Mapping
from re import findall

import os
import oyaml as yaml
from attmap import AttMap
from yacman import YacAttMap


from attmap import PathExAttMap as PXAM

from .const import *
from .exceptions import *
from.helpers import *

from ubiquerg import expandpath, create_lock, remove_lock

_LOGGER = getLogger(PKG_NAME)


class LoggingCursor(psycopg2.extras.DictCursor):
    """
    Logging db cursor
    """

    def execute(self, query, vars=None):
        """
        Execute a database operation (query or command) and issue a debug
        and info level log messages

        :param query:
        :param vars:
        :return:
        """
        _LOGGER.debug(f"Executing query: {self.mogrify(query, vars)}")
        try:
            super(LoggingCursor, self).execute(query=query, vars=vars)
        except Exception as e:
            _LOGGER.error(f"{e.__class__.__name__}: {e}")
            raise
        else:
            _LOGGER.debug(f"Executed query: {self.query}")


class PipestatManager(AttMap):
    """
    Class that provides methods for a standardized reporting of pipeline
    statistics. It formalizes a way for pipeline developers and downstream
    tools developers to communicate -- results produced by a pipeline can
    easily and reliably become an input for downstream analyses.
    """
    def __init__(self, name, schema_path, db_file_path=None, db_config_path=None):
        """
        Initialize the object

        :param str name: namespace to report into. This will be the DB table
            name if using DB as the object back-end
        :param str schema_path: path to the output schema that formalizes
            the results structure
        :param str db_file_path: YAML file to report into, if file is used as
            the object back-end
        :param db_config_path: DB login credentials to report into, if DB is
            used as the object back-end
        """
        def _check_cfg_key(cfg, key):
            if key not in cfg:
                _LOGGER.warning(f"Key '{key}' not found in config")
                return False
            return True

        def _read_yaml_data(path, what):
            assert isinstance(path, str), \
                TypeError(f"Path is not a string: {path}")
            path = expandpath(path)
            assert os.path.exists(path), \
                FileNotFoundError(f"File not found: {path}")
            _LOGGER.info(f"Reading {what} from '{path}'")
            with open(path, "r") as f:
                return path, yaml.safe_load(f)

        super(PipestatManager, self).__init__()

        self[NAME_KEY] = str(name)
        self[FILE_KEY] = None
        self[DATA_KEY] = None
        _, self[SCHEMA_KEY] = _read_yaml_data(schema_path, "schema")
        validate_schema(self.schema)
        if db_file_path:
            self[FILE_KEY] = expandpath(db_file_path)
            _LOGGER.info(f"Reading data from: '{self.file}'")
            self[DATA_KEY] = YacAttMap(filepath=self.file)
        elif db_config_path:
            _, self[CONFIG_KEY] = _read_yaml_data(db_config_path, "DB config")
            if not all([_check_cfg_key(self[CONFIG_KEY], key) for key in DB_CREDENTIALS]):
                raise MissingConfigDataError(
                    "Must specify all database login credentials")
            self._init_postgres_table()
        else:
            raise MissingConfigDataError("Must specify either database login "
                                         "credentials or a YAML file path")

    def __str__(self):
        """
        Generate string representation of the object

        :return str: string representation of the object
        """
        res = f"{self.__class__.__name__} ({self.name})"
        records_count = len(self[DATA_KEY]) if self.file \
            else self._count_rows(table_name=self.name)
        res += "\nBackend: {}".format(
            f"file ({self.file})" if self.file else "PostgreSQL")
        res += f"\nRecords count: {records_count}"
        return res

    @property
    def name(self):
        """
        Namespace the object writes the results to

        :return str: Namespace the object writes the results to
        """
        return self._name

    @property
    def schema(self):
        """
        Schema mapping

        :return dict: schema that formalizes the results structure
        """
        return self[SCHEMA_KEY]

    @property
    def file(self):
        """
        File path that the object is reporting the results into

        :return str: file path that the object is reporting the results into
        """
        return self[FILE_KEY]

    @property
    def data(self):
        """
        Data object

        :return yacman.YacAttMap: the object that stores the reported data
        """
        return self[DATA_KEY]

    @property
    @contextmanager
    def db_cursor(self):
        """
        Establish connection and get a PostgreSQL database cursor,
        commit and close the connection afterwards

        :return DictCursor: Database cursor object
        """
        try:
            if not self.check_connection():
                self.establish_postgres_connection()
            with self[DB_CONNECTION_KEY] as c, \
                    c.cursor(cursor_factory=LoggingCursor) as cur:
                yield cur
        except:
            raise
        finally:
            self.close_postgres_connection()

    def _init_postgres_table(self):
        """
        Initialize postgreSQL table based on the provided schema

        :return bool: whether the table has be created successfully
        """
        if self._check_table_exists(table_name=self.name):
            _LOGGER.warning(
                f"Table '{self.name}' already exists in the database")
            return False
        _LOGGER.info(
            f"Initializing '{self.name}' table in '{PKG_NAME}' database")
        columns = FIXED_COLUMNS.append(schema_to_columns(schema=self.schema))
        with self.db_cursor as cur:
            s = sql.SQL(f"CREATE TABLE {self.name} ({','.join(columns)})")
            cur.execute(s)
        return True

    def _check_table_exists(self, table_name):
        """
        Check if the specified table exists

        :param str table_name: table name to be checked
        :return bool: whether the specified table exists
        """
        with self.db_cursor as cur:
            cur.execute(
                "SELECT EXISTS(SELECT * FROM information_schema.tables "
                "WHERE table_name=%s)",
                (table_name, )
            )
            return cur.fetchone()[0]

    def _check_record(self, condition_col, condition_val):
        """
        Check if the record matching the condition is in the table

        :param str condition_col: column to base the check on
        :param str condition_val: value in the selected column
        :return bool: whether any record matches the provided condition
        """
        with self.db_cursor as cur:
            statement = f"SELECT EXISTS(SELECT 1 from {self.name} " \
                        f"WHERE {condition_col}=%s)"
            cur.execute(statement, (condition_val, ))
            return cur.fetchone()[0]

    def _count_rows(self, table_name):
        """
        Count rows in a selected table

        :param str table_name: table to count rows for
        :return int: number of rows in the selected table
        """
        with self.db_cursor as cur:
            statement = sql.SQL("SELECT COUNT(*) FROM {}").format(
                sql.Identifier(table_name))
            cur.execute(statement)
            return cur.fetchall()[0][0]

    def _report_postgres(self, value, record_identifier):
        """
        Check if record with this record identifier in table, create new record
         if not (INSERT), update the record if yes (UPDATE).

        Currently supports just one column at a time.

        :param str record_identifier: unique identifier of the record, value to
            in 'record_identifier' column to look for to determine if the record
            already exists in the table
        :param dict value: a mapping of pair of table column name and
            respective value to be inserted to the database
        :return int: id of the row just inserted
        """
        # TODO: allow multi-value insertions
        # placeholder = sql.SQL(','.join(['%s'] * len(value)))
        # TODO: allow returning updated/inserted record ID
        if not self._check_record(condition_col="record_identifier",
                                  condition_val=record_identifier):
            with self.db_cursor as cur:
                cur.execute(
                    f"INSERT INTO {self.name} (record_identifier) VALUES (%s)",
                    (record_identifier, )
                )
        column = list(value.keys())
        assert len(column) == 1, \
            NotImplementedError("Can't report more than one column at once")
        value = list(value.values())[0]
        query = "UPDATE {table_name} SET {column}=%s " \
                "WHERE record_identifier=%s"
        statement = sql.SQL(query).format(
            column=sql.Identifier(column[0]),
            table_name=sql.Identifier(self.name)
        )
        # convert mappings to JSON for postgres
        values = Json(value) if isinstance(value, Mapping) else value
        with self.db_cursor as cur:
            cur.execute(statement, (values, record_identifier))

    def report(self, record_identifier, result_identifier, value):
        """
        Report a result.

        :param str record_identifier: unique identifier of the record, value to
            in 'record_identifier' column to look for to determine if the record
            already exists
        :param any value: value to be reported
        :param str result_identifier: name of the result to be reported
        :return:
        """
        # TODO: add overwrite?
        known_results = self.schema[SCHEMA_PROP_KEY].keys()
        if result_identifier not in known_results:
            raise SchemaError(
                f"'{result_identifier}' is not a known result. Results defined "
                f"in the schema are: {known_results}.")
        attrs = ATTRS_BY_TYPE[
            self.schema[SCHEMA_PROP_KEY][result_identifier][SCHEMA_TYPE_KEY]]
        if attrs:
            if not (isinstance(value, Mapping) or
                    all([attr in value for attr in attrs])):
                raise ValueError(
                    f"Result value to insert is missing at least one of the "
                    f"required attributes: {attrs}")
        if self.file:
            if self.name in self.data and \
                    record_identifier in self.data[self.name] and \
                    result_identifier in self.data[self.name][record_identifier]:
                _LOGGER.warning(
                    f"'{result_identifier}' already in database for "
                    f"'{record_identifier}' in '{self.name}' namespace")
                return False
            self.data.make_writable()
            self[DATA_KEY].setdefault(self.name, PXAM())
            self[DATA_KEY][self.name].setdefault(record_identifier, PXAM())
            self[DATA_KEY][self.name][record_identifier][result_identifier] = \
                value
            self.data.write()
            self.data.make_readonly()
        else:
            self._report_postgres(value={result_identifier: value},
                                  record_identifier=record_identifier)
        _LOGGER.info(
            f"Reported record for '{record_identifier}': {result_identifier}="
            f"{value} in '{self.name}' namespace")
        return True

    def check_connection(self):
        """
        Check whether a PostgreSQL connection has been established

        :return bool: whether the connection has been established
        """
        if self.file is not None:
            raise PipestatDatabaseError(f"The {self.__class__.__name__} object "
                                        f"is not backed by a database")
        if hasattr(self, DB_CONNECTION_KEY) and isinstance(
                getattr(self, DB_CONNECTION_KEY), psycopg2.extensions.connection):
            return True
        return False

    def establish_postgres_connection(self, suppress=False):
        """
        Establish PostgreSQL connection using the config data

        :param bool suppress: whether to suppress any connection errors
        :return bool: whether the connection has been established successfully
        """
        if self.check_connection():
            raise PipestatDatabaseError(f"Connection is already established: "
                                        f"{self[DB_CONNECTION_KEY].info.host}")
        try:
            self[DB_CONNECTION_KEY] = psycopg2.connect(
                dbname=self[CONFIG_KEY][CFG_DB_NAME_KEY],
                user=self[CONFIG_KEY][CFG_DB_USER_KEY],
                password=self[CONFIG_KEY][CFG_DB_PASSWORD_KEY],
                host=self[CONFIG_KEY][CFG_DB_HOST_KEY],
                port=self[CONFIG_KEY][CFG_DB_PORT_KEY]
            )
        except psycopg2.Error as e:
            _LOGGER.error(f"Could not connect to: "
                          f"{self[CONFIG_KEY][CFG_DB_HOST_KEY]}")
            _LOGGER.info(f"Caught error: {e}")
            if suppress:
                return False
            raise
        else:
            _LOGGER.debug(f"Established connection with PostgreSQL: "
                          f"{self[CONFIG_KEY][CFG_DB_HOST_KEY]}")
            return True

    def close_postgres_connection(self):
        """
        Close connection and remove client bound
        """
        if not self.check_connection():
            raise PipestatDatabaseError(
                f"The connection has not been established: "
                f"{self[CONFIG_KEY][CFG_DB_HOST_KEY]}")
        self[DB_CONNECTION_KEY].close()
        del self[DB_CONNECTION_KEY]
        _LOGGER.debug(f"Closed connection with PostgreSQL: "
                      f"{self[CONFIG_KEY][CFG_DB_HOST_KEY]}")
