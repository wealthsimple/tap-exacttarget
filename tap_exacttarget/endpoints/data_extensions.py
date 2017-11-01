import FuelSDK
import singer

from funcy import set_in, update_in, merge

from tap_exacttarget.client import request, request_from_cursor
from tap_exacttarget.dao import DataAccessObject
from tap_exacttarget.pagination import get_date_page, before_today, \
    increment_date
from tap_exacttarget.state import incorporate, save_state, \
    get_last_record_value_for_table
from tap_exacttarget.util import sudsobj_to_dict

LOGGER = singer.get_logger()  # noqa


def _merge_in(collection, path, new_item):
    return update_in(collection, path, lambda x: merge(x, [new_item]))


def _convert_extension_datatype(datatype):
    if datatype in ['Boolean']:
        return 'bool'
    elif datatype in ['Decimal', 'Number']:
        return 'number'

    return 'string'


def _convert_data_extension_to_catalog(extension):
    return {
        field.get('Name'): {
            'type': _convert_extension_datatype(field.get('ValueType')),
            'description': field.get('Description'),
            'inclusion': 'available',
        }
        for field in extension.get('Fields')
    }


def _get_tap_stream_id(extension):
    extension_name = extension.CustomerKey
    return 'data_extension.{}'.format(extension_name)


def _get_extension_name_from_tap_stream_id(tap_stream_id):
    return tap_stream_id.split('.')[1]


class DataExtensionDataAccessObject(DataAccessObject):

    @classmethod
    def matches_catalog(cls, catalog):
        return 'data_extension.' in catalog.get('stream')

    def _get_extensions(self):
        result = request(
            'DataExtension',
            FuelSDK.ET_DataExtension,
            self.auth_stub,
            props=['CustomerKey', 'Name'])

        to_return = {}

        for extension in result:
            extension_name = str(extension.Name)
            customer_key = str(extension.CustomerKey)

            to_return[customer_key] = {
                'tap_stream_id': 'data_extension.{}'.format(customer_key),
                'stream': 'data_extension.{}'.format(extension_name),
                'key_properties': ['_CustomObjectKey'],
                'schema': {
                    'type': 'object',
                    'inclusion': 'available',
                    'selected': False,
                    'properties': {
                        '_CustomObjectKey': {
                            'type': 'string',
                            'description': ('Hidden auto-incrementing primary '
                                            'key for data extension rows.'),
                        },
                        'CategoryID': {
                            'type': 'integer',
                            'description': ('Specifies the identifier of the '
                                            'folder. (Taken from the parent '
                                            'data extension.)')
                        }
                    }
                },
                'replication_key': 'ModifiedDate',
            }

        return to_return

    def _get_fields(self, extensions):
        to_return = extensions.copy()

        result = request(
            'DataExtensionField',
            FuelSDK.ET_DataExtension_Column,
            self.auth_stub)

        for field in result:
            extension_id = field.DataExtension.CustomerKey
            field = sudsobj_to_dict(field)
            field_name = field['Name']

            if field.get('IsPrimaryKey'):
                to_return = _merge_in(
                    to_return,
                    [extension_id, 'key_properties'],
                    field_name)

            field_schema = {
                'type': [
                    'null',
                    _convert_extension_datatype(str(field.get('FieldType')))
                ],
                'description': str(field.get('Description')),
            }

            to_return = set_in(
                to_return,
                [extension_id, 'schema', 'properties', field_name],
                field_schema)

        return to_return

    def generate_catalog(self):
        # get all the data extensions by requesting all the fields
        extensions_catalog = self._get_extensions()

        extensions_catalog_with_fields = self._get_fields(extensions_catalog)

        return extensions_catalog_with_fields.values()

    def parse_object(self, obj):
        properties = obj.get('Properties', {}).get('Property', {})
        to_return = {}

        for prop in properties:
            to_return[prop['Name']] = prop['Value']

        return to_return

    def _replicate(self, customer_key, keys,
                   parent_category_id, table,
                   partial=False, start=None,
                   end=None, unit=None, replication_key=None):
        if partial:
            LOGGER.info("Fetching {} from {} to {}"
                        .format(table, start, end))

        cursor = FuelSDK.ET_DataExtension_Row()
        cursor.auth_stub = self.auth_stub
        cursor.CustomerKey = customer_key
        cursor.props = keys

        if partial:
            cursor.search_filter = get_date_page(replication_key,
                                                 start,
                                                 unit)

        result = request_from_cursor('DataExtensionObject', cursor)

        for row in result:
            row = self.filter_keys_and_parse(row)
            row['CategoryID'] = parent_category_id

            self.state = incorporate(self.state,
                                     table,
                                     replication_key,
                                     row.get(replication_key))

            singer.write_records(table, [row])

        if partial:
            self.state = incorporate(self.state,
                                     table,
                                     replication_key,
                                     start)

            save_state(self.state)

    def sync_data(self):
        tap_stream_id = self.catalog.get('tap_stream_id')
        table = self.catalog.get('stream')
        (_, customer_key) = tap_stream_id.split('.', 1)

        keys = self.get_catalog_keys()
        keys.remove('CategoryID')

        replication_key = None

        start = get_last_record_value_for_table(self.state, table)

        if start is None:
            start = self.config.get('default_start_date')

        for key in self.config.get('data_extensions', {}) \
                              .get('replication_keys', ['ModifiedDate']):
            if key in keys:
                replication_key = key

        unit = self.config.get('pagination', {}) \
                          .get('data_extension', {'days': 1})

        end = increment_date(start, unit)

        parent_result = None
        parent_extension = None
        parent_result = request(
            'DataExtension',
            FuelSDK.ET_DataExtension,
            self.auth_stub,
            search_filter={
                'Property': 'CustomerKey',
                'SimpleOperator': 'equals',
                'Value': customer_key,
            },
            props=['CustomerKey', 'CategoryID'])

        parent_extension = next(parent_result)
        parent_category_id = parent_extension.CategoryID

        while before_today(start) or replication_key is None:
            self._replicate(
                customer_key,
                keys,
                parent_category_id,
                table,
                partial=(replication_key is not None),
                start=start,
                end=end,
                replication_key=replication_key)

            if replication_key is None:
                return

            self.state = incorporate(self.state,
                                     table,
                                     replication_key,
                                     start)

            save_state(self.state)

            start = end
            end = increment_date(start, unit)
