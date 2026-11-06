"""Use a Cardinal checkout with its dependencies; no real network calls."""
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

source = Path(os.environ["CARDINAL_SOURCE"]).resolve()
plugin = Path(__file__).resolve().parents[1] / "ad_sync.py"
sys.path.insert(0, str(source))
from cardinal import Cardinal  # noqa: E402
from FunPayAPI.account import Account  # noqa: E402
from FunPayAPI.types import LotFields  # noqa: E402

with tempfile.TemporaryDirectory() as directory:
    previous = Path.cwd()
    try:
        os.chdir(directory)
        Path("plugins").mkdir()
        shutil.copy2(plugin, "plugins/ad_sync.py")
        module, metadata = Cardinal.load_plugin("ad_sync.py")
        assert Cardinal.is_uuid_valid(metadata["UUID"])
        assert callable(metadata["BIND_TO_DELETE"])
        assert callable(module.BIND_TO_NEW_ORDER[0])
        payloads = []
        fields = LotFields(1, {"active": "on", "auto_delivery": "on",
                               "secrets": "last-account", "price": "100"})
        fields.secrets = []
        fields.active = False

        def request(method, path, headers, data, **kwargs):
            payloads.append(dict(data))
            return SimpleNamespace(json=lambda: {})

        account = SimpleNamespace(is_initiated=True, csrf_token="test", method=request)
        Account.save_offer(account, fields)
        assert payloads[0]["secrets"] == ""
        assert payloads[0]["active"] == ""
        assert payloads[0]["auto_delivery"] == "on"
        assert payloads[0]["price"] == "100.0"
        print("Cardinal loader and FunPayAPI empty-stock serialization: PASS", metadata["VERSION"])
    finally:
        os.chdir(previous)
