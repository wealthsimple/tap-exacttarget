import singer

from funcy import project

from tap_exacttarget.util import sudsobj_to_dict

LOGGER = singer.get_logger()


def _get_catalog_schema(catalog):
    return catalog.schema.properties


class DataAccessObject(object):

    def __init__(self, config, state, auth_stub, catalog):
        self.config = config.copy()
        self.state = state.copy()
        self.catalog = catalog
        self.auth_stub = auth_stub

    @classmethod
    def matches_catalog(cls, catalog):
        return catalog.stream == cls.TABLE

    def generate_catalog(self):
        cls = self.__class__

        return [{
            'tap_stream_id': cls.TABLE,
            'stream': cls.TABLE,
            'key_properties': cls.KEY_PROPERTIES,
            'schema': cls.SCHEMA,
        }]

    def filter_keys_and_parse(self, obj):
        to_return = sudsobj_to_dict(obj)

        return self.parse_object(to_return)

    def get_catalog_keys(self):
        return list(
            self.catalog.schema.properties.keys())

    def parse_object(self, obj):
        return project(obj, self.get_catalog_keys())

    def write_schema(self):
        singer.write_schema(
            self.catalog.stream,
            self.catalog.schema.to_dict(),
            key_properties=self.catalog.key_properties)

    def sync(self):
        if not self.catalog.schema.selected:
            LOGGER.info('{} is not marked as selected, skipping.'
                        .format(self.catalog.stream))
            return

        LOGGER.info('Syncing stream {} with accessor {}'
                    .format(self.catalog.tap_stream_id,
                            self.__class__.__name__))

        self.write_schema()

        return self.sync_data()

    # OVERRIDE THESE TO IMPLEMENT A NEW DAO:

    SCHEMA = None
    TABLE = None
    KEY_PROPERTIES = None

    def sync_data(self):  # pylint: disable=no-self-use
        raise RuntimeError('sync_data is not implemented!')
