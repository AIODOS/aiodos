# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""aiodos-client : la bibliothèque Python des applications pour la passerelle locale d'AIODOS
(127.0.0.1:11435), sans aucune dépendance.

    from aiodos_client import Aiodos, Keystore, errors

    ai = Aiodos(app="PANDORA", app_id="arkane.pandora", version="3.2.0",
                keystore=Keystore.dpapi(r"%LOCALAPPDATA%\\PANDORA\\aiodos.key"))
    if ai.status().state != "ready":
        ai.connect(["chat", "embed"], purpose="Traduire les prompts.", on_code=show_code)
    result = ai.chat([{"role": "user", "content": "Bonjour"}], think=False)

La référence est docs/api-applications.md (dépôt d'AIODOS) ; le contrat partagé,
clients/contract/. Journal : le logger « aiodos_client » (NullHandler) ; jamais de print.
"""

import logging

__version__ = "0.1.0"
API_VERSION = 1

logging.getLogger("aiodos_client").addHandler(logging.NullHandler())

# Après les deux constantes : les modules du paquet s'en servent.
from . import errors  # noqa: E402
from .access import Access, AccessRequest  # noqa: E402
from .chat import ChatResult, ToolCall, Vectors  # noqa: E402
from .client import Aiodos  # noqa: E402
from .errors import AiodosError  # noqa: E402
from .keystore import Keystore  # noqa: E402
from .models import Model, ModelList, Placement  # noqa: E402
from .status import Status  # noqa: E402

__all__ = ["API_VERSION", "Access", "AccessRequest", "Aiodos", "AiodosError", "ChatResult",
           "Keystore", "Model", "ModelList", "Placement", "Status", "ToolCall", "Vectors",
           "__version__", "errors"]
