#!/usr/bin/env python3
import argparse
import requests
import sys
import getpass
import base64
from lxml import etree


OSM_API = 'https://api.openstreetmap.org/api/0.6/'
SANDBOX_API = 'https://master.apis.dev.openstreetmap.org/api/0.6/'
OVERPASS_API = 'http://overpass-api.de/api'
CREATED_BY = 'OSM Dev Copy 1.0'
SORT_ORDER = {'node': 0, 'way': 1, 'relation': 2}


class OsmObject:
    def __init__(self, node):
        self.type = node.tag
        self.id = node.get('id')
        self.version = node.get('version')
        self.lon = node.get('lon')
        self.lat = node.get('lat')
        self.tags = {}
        for tag in node.findall('tag'):
            self.tags[tag.get('k')] = tag.get('v')
        self.nodes = [nd.get('ref') for nd in node.findall('nd')]
        self.members = []
        for m in node.findall('member'):
            self.members.append((m.get('type'), m.get('ref'), m.get('role')))

    @property
    def sid(self):
        return self.type + self.id

    @property
    def sort_key(self):
        return (SORT_ORDER[self.type], self.id)

    def is_inside(self, bbox):
        if self.lon is None or self.lat is None:
            return True
        lon = float(self.lon)
        lat = float(self.lat)
        return lon >= bbox[0] and lon <= bbox[2] and lat >= bbox[1] and lat <= bbox[3]

    def delete_xml(self, changeset=None):
        el = etree.Element(
            self.type,
            id=self.id,
            version=self.version,
            changeset=changeset,
            visible='false',
        )
        if changeset:
            el.set('changeset', changeset)
        return el

    def create_xml(self, changeset=None):
        el = etree.Element(
            self.type,
            id=self.id,
            version=self.version,
            changeset=changeset,
            visible='true',
        )
        if changeset:
            el.set('changeset', changeset)
        if self.lon and self.lat:
            el.set('lon', self.lon)
            el.set('lat', self.lat)
        for k, v in self.tags.items():
            etree.SubElement(el, 'tag', k=k, v=v)
        for ref in self.nodes:
            etree.SubElement(el, 'nd', ref=ref)
        for m in self.members:
            etree.SubElement(el, 'member', type=m[0], ref=m[1], role=m[2])
        return el


class Uploader:
    def __init__(self, server, auth, comment):
        self.server = server
        self.auth = auth
        self.comment = comment

    def __enter__(self):
        root = etree.Element('osm')
        changeset = etree.SubElement(root, 'changeset')
        etree.SubElement(changeset, 'tag', k='comment', v=self.comment)
        etree.SubElement(changeset, 'tag', k='created_by', v=CREATED_BY)

        try:
            resp = api_request(
                self.server, 'changeset/create', method='PUT', raw_result=True,
                auth=self.auth, data=etree.tostring(root))
        except HTTPError as e:
            raise IOError('Failed to create a changeset: {} {}'.format(
                e.code, e.message))
        self.changeset = resp.strip()
        return self

    def upload(self, root):
        try:
            resp = api_request(
                self.server, f'changeset/{self.changeset}/upload', method='POST',
                auth=self.auth, data=etree.tostring(root))
        except HTTPError as e:
            raise IOError('Failed to erase data from the sandbox: {} {}'.format(
                e.code, e.message))

        # Return id mapping
        id_map = {}
        for diff in resp:
            id_map[(diff.tag, diff.get('old_id'))] = diff.get('new_id')
        return id_map

    def __exit__(self, type, value, tb):
        try:
            api_request(
                self.server, f'changeset/{self.changeset}/close', method='PUT',
                auth=self.auth, raw_result=True)
        except HTTPError as e:
            print('Failed to close a changeset: {} {}'.format(e.code, e.message))


class HTTPError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message

    def __str__(self):
        return 'HTTPError({}, {})'.format(self.code, self.message)


class AuthPromptAction(argparse.Action):
    def __init__(self,
                 option_strings,
                 dest=None,
                 nargs=0,
                 default=None,
                 required=False,
                 type=None,
                 metavar=None,
                 help=None):
        super(AuthPromptAction, self).__init__(
            option_strings=option_strings,
            dest=dest,
            nargs=nargs,
            default=default,
            required=required,
            metavar=metavar,
            type=type,
            help=help)

    def __call__(self, parser, args, values, option_string=None):
        auth_header = read_auth()
        setattr(args, self.dest, auth_header)


def api_request(server, endpoint, method='GET', params=None,
                raw_result=False, auth=None, **kwargs):
    headers = {}
    headers['Content-Type'] = 'application/xml'
    if auth:
        headers['Authorization'] = auth
    resp = requests.request(method, server + endpoint, params=params, headers=headers, **kwargs)
    resp.encoding = 'utf-8'
    if resp.status_code != 200:
        raise HTTPError(resp.status_code, resp.text)
    if resp.content and not raw_result:
        return etree.fromstring(resp.content)
    return resp.text


def read_auth():
    """Read login and password from keyboard, and prepare a basic auth header."""
    ok = False
    while not ok:
        login = input('Login: ')
        if not login:
            print('Okay')
            sys.exit(0)
        auth_header = 'Basic {0}'.format(base64.b64encode('{0}:{1}'.format(
            login, getpass.getpass('Password: ')).encode('utf-8')).decode('utf-8'))
        try:
            result = api_request(SANDBOX_API, 'user/details', auth=auth_header)
            ok = len(result) > 0
        except HTTPError:
            pass
        if not ok:
            print('You must have mistyped. Please try again.')
    return auth_header


def get_changeset_size(endpoint):
    data = api_request(endpoint, 'capabilities')
    try:
        return int(data.find('api').find('changesets').get('maximum_elements'))
    except AttributeError:
        print('Failed to get maximum changeset size.')
        return 10000


def split_bbox(bbox):
    half_x = (bbox[0] + bbox[2]) / 2
    half_y = (bbox[1] + bbox[3]) / 2
    return [
        [bbox[0], bbox[1], half_x, half_y],
        [bbox[0], half_y, half_x, bbox[3]],
        [half_x, bbox[1], bbox[2], half_y],
        [half_x, half_y, bbox[2], bbox[3]],
    ]


def download_from_api(bbox, endpoint):
    try:
        tree = api_request(endpoint, 'map', params={'bbox': ','.join(str(x) for x in bbox)})
        lst = [OsmObject(obj) for obj in tree if obj.tag in ('node', 'way', 'relation')]
        return {obj.sid: obj for obj in lst}
    except HTTPError as e:
        if e.code == 400:
            # Area too large, split bbox in four
            data = {}
            for part in split_bbox(bbox):
                more_data = download_from_api(part, endpoint)
                data.update(more_data)
            return data
        elif e.code == 509:
            raise Exception('You have been blocked from API for downloading too much: ' + e.message)


def download_from_overpass(bbox, overpass_api, filter_str=None, date_str=None):
    bbox_para = ','.join(str(bbox[i]) for i in (1, 0, 3, 2))
    date_para = f'[date:"{date_str}"]' if date_str else ""
    filter_para = f'[{filter_str}]' if filter_str else ""
    query = (f'[timeout:300]{date_para}[bbox:{bbox_para}];'
             f'(nwr{filter_para};);'
             '(_.;>;);'
             # 'nwr._;' # This will produce results equivalent to the former version of the tool but may destroy larger objects.
             'out meta qt;')
    resp = requests.get(f'{overpass_api}/interpreter', {'data': query})
    if resp.status_code != 200:
        if 'rate_limited' in resp.text:
            resp = requests.get(f'{overpass_api}/status')
            print(resp.text)
            raise Exception('You are rate limited')
        raise Exception('Could not download data from Overpass API: ' + resp.text)
    tree = etree.fromstring(resp.content)
    lst = [OsmObject(obj) for obj in tree if obj.tag in ('node', 'way', 'relation')]
    return {obj.sid: obj for obj in lst}


def filter_by_bbox(elements, bbox):
    for sid in list(elements.keys()):
        el = elements[sid]
        if not el.is_inside(bbox):
            del elements[sid]
        elif any(1 for m in el.members if m[0] == 'relation'):
            # Noping out of relations inside relations
            del elements[sid]


def delete_missing(elements):
    nodes = set(el.id for el in elements.values() if el.type == 'node')
    for sid in list(elements.keys()):
        if any(1 for ref in elements[sid].nodes if ref not in nodes):
            del elements[sid]

    ways = [el.id for el in elements.values() if el.type == 'way']
    for sid in list(elements.keys()):
        for m in elements[sid].members:
            if ((m[0] == 'node' and m[1] not in nodes) or
                    (m[0] == 'way' and m[1] not in ways)):
                del elements[sid]
                break


def delete_unreferenced_nodes(elements):
    nodes = set()
    for el in elements.values():
        nodes.update(el.nodes)
        nodes.update(m[1] for m in el.members if m[0] == 'node')
    for sid in list(elements.keys()):
        el = elements[sid]
        if el.type == 'node' and el.id not in nodes and not el.tags:
            del elements[sid]


def upload_delete(elements, auth_header):
    with Uploader(SANDBOX_API, auth_header, 'Clearing an area before uploading') as u:
        root = etree.Element('osmChange', version='0.6', generator=CREATED_BY)
        delete = etree.SubElement(root, 'delete')
        delete.set('if-unused', 'true')
        for el in elements:
            delete.append(el.delete_xml(u.changeset))
        u.upload(root)


def delete_elements(elements, auth_header):
    max_el = get_changeset_size(SANDBOX_API)
    values = list(elements.values())
    values.sort(key=lambda el: el.sort_key, reverse=True)
    for i in range(0, len(values), max_el):
        upload_delete(values[i:i + max_el], auth_header)


def upload_create(elements, auth_header):
    with Uploader(SANDBOX_API, auth_header, 'Copying data from OSM') as u:
        root = etree.Element('osmChange', version='0.6', generator=CREATED_BY)
        create = etree.SubElement(root, 'create')
        for el in elements:
            create.append(el.create_xml(u.changeset))
        return u.upload(root)


def renumber(values, id_map):
    for v in values:
        if (v.type, v.id) in id_map:
            v.id = str(id_map[(v.type, v.id)])
        v.nodes = [str(id_map.get(('node', ref), ref)) for ref in v.nodes]
        v.members = [(m[0], str(id_map.get((m[0], m[1]), m[1])), m[2]) for m in v.members]


def renumber_for_creating(values):
    new_id = -1
    id_map = {}
    for v in values:
        id_map[(v.type, v.id)] = new_id
        new_id -= 1
    renumber(values, id_map)


def upload_elements(elements, auth_header):
    max_el = get_changeset_size(OSM_API)
    values = list(elements.values())
    values.sort(key=lambda el: el.sort_key)
    renumber_for_creating(values)
    for i in range(0, len(values), max_el):
        id_map = upload_create(values[i:i + max_el], auth_header)
        renumber(values, id_map)


def write_osc_and_exit(elements, fileobj):
    root = etree.Element('osmChange', version='0.6', generator=CREATED_BY)
    create = etree.SubElement(root, 'create')
    for el in elements.values():
        create.append(el.create_xml('1'))
    fileobj.write(etree.tostring(root, encoding='unicode', pretty_print=True))
    sys.exit(0)


def main(bbox, auth_header, overpass_api=OVERPASS_API, filter_str=None, date_str=None):
    if len(bbox) != 4:
        raise ValueError('Please specify four numbers for the bbox')
    if bbox[0] > bbox[2]:
        bbox[2], bbox[0] = bbox[0], bbox[2]
    if bbox[1] > bbox[3]:
        bbox[3], bbox[1] = bbox[1], bbox[3]
    if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > 0.01:
        raise ValueError('Bounding box is too big, try 10×10 km')

    sandbox_elements = download_from_api(bbox, SANDBOX_API)
    if len(sandbox_elements) > 10000:
        print(f'Sandbox has {len(sandbox_elements)} elements at this location.')
        print('Proceed with deleting them? (type "yes" if agreed)')
        answer = input()
        if answer.lower() != 'yes':
            sys.exit(0)
    if not sandbox_elements:
        print('Sandbox is empty there.')
    else:
        print('Clearing the area on the sandbox server.')
        delete_elements(sandbox_elements, auth_header)

    elements = download_from_overpass(bbox,
                                      overpass_api,
                                      filter_str=filter_str,
                                      date_str=date_str)
    # filter_by_bbox(elements, bbox)
    # delete_missing(elements)
    # delete_unreferenced_nodes(elements)

    if not elements:
        print('No elements in the given bounding box')
        return
    print(f'Downloaded {len(elements)} elements.')

    # write_osc_and_exit(elements, open('test.osc', 'w'))

    print('Uploading new data.')
    upload_elements(elements, auth_header)

    print('Done.')


def cli():
    parser = argparse.ArgumentParser(
        description="Downloads data from Overpass API and uploads it to the mapping "
                    "sandbox.",
        epilog="Because sandboxes are for grown-ups, too!",
    )
    parser = add_args(parser)
    args = parser.parse_args()
    bbox = [float(x.strip()) for x in args.bbox.split(',')]
    main(bbox, args.auth_header, args.overpass_api)


def add_args(parser):
    parser.add_argument(
        "bbox",
        help="The target bounding box in format minlon,minlat,maxlon,maxlat. "
             "Get the bounding box from https://boundingbox.klokantech.com/.",
    )
    parser.add_argument("--auth", "-a",
                        required=True,
                        help="This flag will spawn a password prompt before entering "
                             "the program. Authentication is necessary to upload data "
                             "to the sandbox.",
                        dest='auth_header',
                        action=AuthPromptAction,
                        type=str)
    parser.add_argument("--overpass",
                        required=False,
                        help="Use a custom overpass API instance "
                             f"(default: {OVERPASS_API}).",
                        dest='overpass_api',
                        default=OVERPASS_API,
                        type=str)
    return parser


if __name__ == '__main__':
    cli()
