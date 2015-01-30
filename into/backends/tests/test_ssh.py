import pytest
paramiko = pytest.importorskip('paramiko')

import re
import os

from into.utils import tmpfile, filetext
from into.directory import _Directory, Directory
from into.backends.ssh import SSH, resource, ssh_pattern, sftp, drop
from into.backends.csv import CSV
from into import into, discover

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

try:
    ssh.connect('localhost')
except:
    pytest.skip('could not connect to localhost')


def test_resource():
    r = resource('ssh://joe@localhost:/path/to/myfile.csv')
    assert isinstance(r, SSH(CSV))
    assert r.path == '/path/to/myfile.csv'
    assert r.auth['hostname'] == 'localhost'
    assert r.auth['username'] == 'joe'


def test_resource_directory():
    r = resource('ssh://joe@localhost:/path/to/')
    assert issubclass(r.subtype, _Directory)

    r = resource('ssh://joe@localhost:/path/to/*.csv')
    assert r.subtype == Directory(CSV)
    assert r.path == '/path/to/'


def test_discover():
    with filetext('name,balance\nAlice,100\nBob,200') as fn:
        local = CSV(fn)
        remote = SSH(CSV)(fn, hostname='localhost')

        assert discover(local) == discover(remote)


def test_discover_from_resource():
    with filetext('name,balance\nAlice,100\nBob,200', extension='csv') as fn:
        local = CSV(fn)
        remote = resource('ssh://localhost:' + fn)

        assert discover(local) == discover(remote)


def test_ssh_pattern():
    uris = ['localhost:myfile.csv',
            '127.0.0.1:/myfile.csv',
            'user@127.0.0.1:/myfile.csv',
            'user@127.0.0.1:/*.csv',
            'user@127.0.0.1:/my-dir/my-file3.csv']
    for uri in uris:
        assert re.match(ssh_pattern, uri)


def test_copy_remote_csv():
    with tmpfile('csv') as target:
        with filetext('name,balance\nAlice,100\nBob,200',
                      extension='csv') as fn:
            csv = resource(fn)
            scsv = into('ssh://localhost:foo.csv', csv)
            assert isinstance(scsv, SSH(CSV))
            assert discover(scsv) == discover(csv)

            # Round trip
            csv2 = into(target, scsv)
            assert into(list, csv) == into(list, csv2)


def test_drop():
    with filetext('name,balance\nAlice,100\nBob,200', extension='csv') as fn:
        with tmpfile('csv') as target:
            scsv = SSH(CSV)(target, hostname='localhost')

            assert not os.path.exists(target)

            with sftp(**scsv.auth) as conn:
                conn.put(fn, target)

            assert os.path.exists(target)

            drop(scsv)

            assert not os.path.exists(target)
