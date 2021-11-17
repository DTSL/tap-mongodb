# Discovery method
import json
import sys

import singer
from singer import metadata

LOGGER = singer.get_logger()


IGNORE_DBS = ['system', 'local', 'config']
ROLES_WITHOUT_FIND_PRIVILEGES = {
    'dbAdmin',
    'userAdmin',
    'clusterAdmin',
    'clusterManager',
    'clusterMonitor',
    'hostManager',
    'restore'
}
ROLES_WITH_FIND_PRIVILEGES = {
    'read',
    'readWrite',
    'readAnyDatabase',
    'readWriteAnyDatabase',
    'dbOwner',
    'backup',
    'root'
}
ROLES_WITH_ALL_DB_FIND_PRIVILEGES = {
    'readAnyDatabase',
    'readWriteAnyDatabase',
    'root'
}


def do_discover(client, config):
    streams = []

    for db_name in get_databases(client, config):
        # pylint: disable=invalid-name
        db = client[db_name]

        collection_names = db.list_collection_names()
        for collection_name in [c for c in collection_names
                                if not c.startswith("system.")]:

            collection = db[collection_name]
            is_view = collection.options().get('viewOn') is not None
            # TODO: Add support for views
            if is_view:
                continue

            LOGGER.info("Getting collection info for db: %s, collection: %s",
                        db_name, collection_name)
            streams.append(produce_collection_schema(collection))

    json.dump({'streams' : streams}, sys.stdout, indent=2)

def get_databases(client, config):
    roles = get_roles(client, config)
    LOGGER.info('Roles: %s', roles)

    can_read_all = len([r for r in roles if r['role'] in ROLES_WITH_ALL_DB_FIND_PRIVILEGES]) > 0

    if can_read_all:
        db_names = [d for d in client.list_database_names() if d not in IGNORE_DBS]
    else:
        db_names = [r['db'] for r in roles if r['db'] not in IGNORE_DBS]
    db_names = list(set(db_names))  # Make sure each db is only in the list once
    LOGGER.info('Datbases: %s', db_names)
    return db_names

def get_roles(client, config):
    # usersInfo Command returns object in shape:
    # {
    #     <some_other_keys>
    #     'users': [
    #         {
    #             '_id': <auth_db>.<user>,
    #             'db': <auth_db>,
    #             'mechanisms': ['SCRAM-SHA-1', 'SCRAM-SHA-256'],
    #             'roles': [{'db': 'admin', 'role': 'readWriteAnyDatabase'},
    #                       {'db': 'local', 'role': 'read'}],
    #             'user': <user>,
    #             'userId': <userId>
    #         }
    #     ]
    # }
    user_info = client[config['database']].command({'usersInfo': config['user']})

    users = [u for u in user_info.get('users') if u.get('user') == config['user']]
    if len(users) != 1:
        LOGGER.warning('Could not find any users for %s', config['user'])
        return []

    roles = []
    for role in users[0].get('roles', []):
        if role.get('role') is None:
            continue

        role_name = role['role']
        # roles without find privileges
        if role_name in ROLES_WITHOUT_FIND_PRIVILEGES:
            continue

        # roles with find privileges
        if role_name in ROLES_WITH_FIND_PRIVILEGES:
            if role.get('db'):
                roles.append(role)

        # for custom roles, get the "sub-roles"
        else:
            role_info_list = client[config['database']].command(
                {'rolesInfo': {'role': role_name, 'db': config['database']}})
            role_info = [r for r in role_info_list.get('roles', []) if r['role'] == role_name]
            if len(role_info) != 1:
                continue
            for sub_role in role_info[0].get('roles', []):
                if sub_role.get('role') in ROLES_WITH_FIND_PRIVILEGES:
                    if sub_role.get('db'):
                        roles.append(sub_role)
    return roles

def produce_collection_schema(collection):
    collection_name = collection.name
    collection_db_name = collection.database.name

    is_view = collection.options().get('viewOn') is not None

    mdata = {}
    mdata = metadata.write(mdata, (), 'table-key-properties', ['_id'])
    mdata = metadata.write(mdata, (), 'database-name', collection_db_name)
    mdata = metadata.write(mdata, (), 'row-count', collection.estimated_document_count())
    mdata = metadata.write(mdata, (), 'is-view', is_view)

    # write valid-replication-key metadata by finding fields that have indexes on them.
    # cannot get indexes for views -- NB: This means no key-based incremental for views?
    if not is_view:
        valid_replication_keys = []
        coll_indexes = collection.index_information()
        # index_information() returns a map of index_name -> index_information
        for _, index_info in coll_indexes.items():
            # we don't support compound indexes
            if len(index_info.get('key')) == 1:
                index_field_info = index_info.get('key')[0]
                # index_field_info is a tuple of (field_name, sort_direction)
                if index_field_info:
                    valid_replication_keys.append(index_field_info[0])

        if valid_replication_keys:
            mdata = metadata.write(mdata, (), 'valid-replication-keys', valid_replication_keys)

    return {
        'table_name': collection_name,
        'stream': collection_name,
        'metadata': metadata.to_list(mdata),
        'tap_stream_id': "{}-{}".format(collection_db_name, collection_name),
        'schema': {
            'type': 'object'
        }
    }
