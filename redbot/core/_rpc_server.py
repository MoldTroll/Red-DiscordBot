import inspect

from aiohttp_json_rpc import JsonRpc
from aiohttp_json_rpc.rpc import JsonRpcMethod

from ._rpc import get_name


class RedRpc(JsonRpc):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_methods(("", self.get_method_info))

    def _add_method(self, method, name="", prefix=""):
        if not inspect.iscoroutinefunction(method):
            return

        name = name or get_name(method, prefix)

        self.methods[name] = JsonRpcMethod(method)

    def remove_method(self, method):
        self.methods.pop(get_name(method), None)

    def remove_methods(self, prefix: str):
        for name in tuple(self.methods):
            if name.startswith(f"{prefix}__"):
                del self.methods[name]

    async def get_method_info(self, request):
        method_name = request.params[0]
        if method_name in self.methods:
            return self.methods[method_name].__doc__
        return "No docstring available."
