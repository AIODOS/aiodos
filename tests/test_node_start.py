"""Démarrage du nœud (aiodos/__main__.py, _Node) : la fenêtre fermée pendant le démarrage
l'interrompt sans que ce soit un échec, et l'arrêt n'attend pas un démarrage abandonné."""
import argparse
import time

import pytest

from aiodos import __main__ as main


def test_closing_the_window_while_starting_is_not_a_failure():
    node = main._Node(argparse.Namespace(), 0, "jeton", "cle")
    node.stop_soon()                                   # la croix, avant même le démarrage
    with pytest.raises(main.StartCancelled):
        node.start()
    assert node.failed is False and node.done.is_set()
    began = time.monotonic()
    node.stop()
    assert time.monotonic() - began < 1
