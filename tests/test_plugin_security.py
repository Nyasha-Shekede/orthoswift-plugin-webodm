import sys
import types
from pathlib import Path

import pytest


def install_webodm_stubs(monkeypatch):
    app=types.ModuleType('app'); plugins=types.ModuleType('app.plugins')
    worker=types.ModuleType('app.plugins.worker')
    class PluginBase: pass
    class Menu:
        def __init__(self,*args): self.args=args
    class MountPoint:
        def __init__(self,*args): self.args=args
    plugins.PluginBase=PluginBase; plugins.Menu=Menu; plugins.MountPoint=MountPoint
    worker.run_function_async=lambda *a,**k: None
    django=types.ModuleType('django'); contrib=types.ModuleType('django.contrib')
    auth=types.ModuleType('django.contrib.auth'); decorators=types.ModuleType('django.contrib.auth.decorators')
    decorators.login_required=lambda f:f
    http=types.ModuleType('django.http'); http.JsonResponse=dict
    shortcuts=types.ModuleType('django.shortcuts'); shortcuts.render=lambda *a,**k: None
    utils=types.ModuleType('django.utils'); translation=types.ModuleType('django.utils.translation'); translation.gettext=lambda x:x
    modules={'app':app,'app.plugins':plugins,'app.plugins.worker':worker,'django':django,
             'django.contrib':contrib,'django.contrib.auth':auth,'django.contrib.auth.decorators':decorators,
             'django.http':http,'django.shortcuts':shortcuts,'django.utils':utils,'django.utils.translation':translation}
    for name,module in modules.items(): monkeypatch.setitem(sys.modules,name,module)


def load_plugin(monkeypatch):
    install_webodm_stubs(monkeypatch)
    sys.modules.pop('orthoswift.plugin',None)
    import orthoswift.plugin as plugin
    return plugin


class Upload:
    def __init__(self,data,name='input.tif'):
        self._data=data; self.name=name; self.size=len(data)
    def open(self,mode='rb'): pass
    def chunks(self):
        yield self._data[:3]; yield self._data[3:]


def test_upload_stream_is_bounded_and_removes_partial_file(tmp_path,monkeypatch):
    plugin=load_plugin(monkeypatch)
    target=tmp_path/'upload.bin'
    with pytest.raises(ValueError,match='limit'):
        plugin._save_uploaded_file(Upload(b'123456'),target,max_bytes=5)
    assert not target.exists()


def test_plugin_manifest_and_assets_are_consistent():
    import json
    root=Path(__file__).parents[1]
    manifest=json.loads((root/'orthoswift/manifest.json').read_text())
    version={}
    exec((root/'orthoswift/version.py').read_text(),version)
    assert manifest['version']==version['__version__']
    assert manifest['repository'].startswith('https://github.com/')
    assert not (root/'orthoswift/public/main.js').exists()
    assert (root/'orthoswift/templates/index.html').exists()
