import sys
import types


def pytest_configure(config):
    """Ensure WebODM and Django mock stubs are available for test suite execution."""
    if "app" not in sys.modules:
        app = types.ModuleType("app")
        plugins = types.ModuleType("app.plugins")
        worker = types.ModuleType("app.plugins.worker")

        class PluginBase:
            def get_path(self):
                return ""

            def get_python_packages_path(self):
                return ""

            def public_url(self, path):
                return f"/public/{path}"

            def template_path(self, path):
                return f"templates/{path}"

        class Menu:
            def __init__(self, *args):
                self.args = args

        class MountPoint:
            def __init__(self, *args):
                self.args = args

        plugins.PluginBase = PluginBase
        plugins.Menu = Menu
        plugins.MountPoint = MountPoint
        worker.run_function_async = lambda *a, **k: None

        django = types.ModuleType("django")
        contrib = types.ModuleType("django.contrib")
        auth = types.ModuleType("django.contrib.auth")
        decorators = types.ModuleType("django.contrib.auth.decorators")
        decorators.login_required = lambda f: f
        http = types.ModuleType("django.http")
        http.JsonResponse = dict
        shortcuts = types.ModuleType("django.shortcuts")
        shortcuts.render = lambda *a, **k: None
        utils = types.ModuleType("django.utils")
        translation = types.ModuleType("django.utils.translation")
        translation.gettext = lambda x: x

        modules = {
            "app": app,
            "app.plugins": plugins,
            "app.plugins.worker": worker,
            "django": django,
            "django.contrib": contrib,
            "django.contrib.auth": auth,
            "django.contrib.auth.decorators": decorators,
            "django.http": http,
            "django.shortcuts": shortcuts,
            "django.utils": utils,
            "django.utils.translation": translation,
        }
        for name, module in modules.items():
            sys.modules.setdefault(name, module)

