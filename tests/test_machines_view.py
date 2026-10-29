"""« Machines connectées » en une seule liste (network/machines_view.py, demandé par l'auteur le
29/10/2026) : chaque machine une fois, sous sa sorte la plus proche (mes machines > amis >
réseau public > réseau local), ses autres chemins dans le détail ; une machine détectée
s'appaire, un appairage en cours se vérifie, une présentation attend sa réponse ; un nœud du
réseau public n'est jamais nommé et ne montre que 8 caractères de sa clé ; le tri met d'abord
ce qui attend une réponse, puis ce qui est en activité, puis le réseau public ; rien de
confidentiel (clé de paire, clé Internet, clé publique entière) n'en sort. Et la vue (Node.js) :
tout ce qui vient d'une autre machine est échappé, dix lignes puis « Voir les N autres »."""
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aiodos import paths
from aiodos.api.server import create_app
from aiodos.network import activity, machines_view, peers, placement, profile, public, public_chat
from aiodos.network import sharing
from tests import i18n_js

NODE = shutil.which("node")
MINE, FRIEND, OTHER = "a" * 32, "b" * 32, "c" * 32
LENDER = "Kq" * 26                     # la clé d'un prêteur du réseau public
STRANGER = "Zx" * 26                   # un nœud qui ne prête pas
MINE_PUBLIC = "Mp" * 26                # la clé publique d'une de mes machines
NET_ID = "n" * 52                      # la clé Internet d'une machine appairée


def _peer(node_id, name, trust="mine", online=True, **extra):
    return dict({"node_id": node_id, "name": name, "trust": trust, "online": online,
                 "address": "192.168.1.20", "port": 47701, "net_id": NET_ID, "last_seen": 1,
                 "profile": {"ollama": {"models": [{"name": "qwen3.5:9b"}, {"name": "gemma3:4b"},
                                                   {"name": "mistral:7b"}]},
                             "lends_models": True,
                             "power": {"mobile": True, "plugged": True, "level": 84}},
                 "live": {"route": "lan", "rtt_ms": 4.2, "sharing": {"enabled": True, "active": True},
                          "for_me": [], "copies_for_me": [], "for_it": [], "chats_for_me": 0,
                          "chats_for_it": 0}}, **extra)


def _node(key, lends=True, free=1, seen_s=3, models=("qwen3.5:9b",)):
    return {"key": key, "version": "1.0.4", "models": list(models), "lends": lends, "free": free,
            "seen_s": seen_s}


def _snap(**parts):
    snap = {"peers": [], "visible": [], "pairing": [], "introductions": [],
            "public": {"key": "Ma" * 26, "nodes": []}}
    snap.update(parts)
    return snap


def _by_id(machines):
    return {m["id"]: m for m in machines}


# ------------------------------------------------------------- chaque machine une fois
def test_a_machine_paired_and_in_the_public_directory_appears_once():
    snap = _snap(peers=[_peer(MINE, "PC fixe")],
                 public={"key": "Ma" * 26, "nodes": [_node(MINE_PUBLIC), _node(LENDER)]})
    machines = machines_view.build(snap, {MINE: {"address": "192.168.1.21", "port": 47701}},
                                   {MINE: MINE_PUBLIC})
    assert [m["id"] for m in machines] == ["public:KqKqKqKq", f"peer:{MINE}"]
    mine = _by_id(machines)[f"peer:{MINE}"]
    assert mine["kind"] == "mine" and mine["name"] == "PC fixe"
    assert mine["paths"] == ["lan", "public"]                  # ses autres chemins
    assert mine["public"]["short"] == MINE_PUBLIC[:8] and mine["address"] == "192.168.1.21"
    assert mine["state"] == "free" and mine["models"][:2] == ["qwen3.5:9b", "gemma3:4b"]
    assert mine["models_n"] == 3 and mine["power"] == {"mobile": True, "plugged": True, "level": 84}
    assert mine["actions"] == ["refresh", "options", "unpair"]
    # Sans la clé qu'elle a dite (un ami, une version d'avant) : deux lignes, l'inconnu à part
    twice = machines_view.build(snap)
    assert {m["id"] for m in twice} == {f"peer:{MINE}", f"public:{MINE_PUBLIC[:8]}", "public:KqKqKqKq"}


def test_a_detected_machine_can_be_paired_and_a_pairing_is_checked_on_its_line():
    seen = {"node_id": OTHER, "name": "Tablette", "address": "192.168.1.30", "port": 47701,
            "last_seen": 5}
    [local] = machines_view.build(_snap(visible=[seen]))
    assert local["kind"] == "local" and local["state"] == "detected"
    assert local["actions"] == ["pair"] and local["node_id"] == OTHER and local["paths"] == ["lan"]
    request = {"node_id": OTHER, "name": "Tablette", "code": "123456", "ref": "r" * 16,
               "address": "192.168.1.30", "internet": False, "known": None, "local_ok": False,
               "remote_ok": True}
    [asked] = machines_view.build(_snap(visible=[seen], pairing=[request]))
    assert asked["id"] == local["id"]                          # la même ligne, pas une autre
    assert asked["state"] == "confirm" and asked["attention"] and asked["actions"] == ["check_code"]
    assert asked["pairing"]["code"] == "123456" and asked["pairing"]["ref"] == "r" * 16
    [waiting] = machines_view.build(_snap(visible=[seen], pairing=[dict(request, local_ok=True)]))
    assert waiting["state"] == "pairing" and waiting["actions"] == [] and not waiting["attention"]
    # Sur invitation, par Internet : une ligne à part, avec les amis
    [invited] = machines_view.build(_snap(pairing=[dict(request, node_id=FRIEND, internet=True)]))
    assert invited["kind"] == "friend" and invited["paths"] == ["internet"]
    assert invited["actions"] == ["check_code"] and invited["address"] == ""


def test_an_introduction_waits_with_the_trust_of_its_presenter():
    offer = {"intro": "i1", "machine": "Portable de Léa", "presenter": "PC de Léa",
             "presenter_id": FRIEND, "created": 10}
    snap = _snap(peers=[_peer(FRIEND, "PC de Léa", trust="friend")], introductions=[offer])
    intro = _by_id(machines_view.build(snap))["intro:i1"]
    assert intro["kind"] == "friend" and intro["state"] == "introduced" and intro["attention"]
    assert intro["actions"] == ["answer_intro"] and intro["intro"]["presenter"] == "PC de Léa"
    snap["peers"][0]["trust"] = "mine"
    assert _by_id(machines_view.build(snap))["intro:i1"]["kind"] == "mine"


def test_a_set_aside_or_offline_machine_keeps_its_actions():
    aside = _peer(FRIEND, "PC de Léa", trust="friend", set_aside_at=5, set_aside_reason="x")
    gone = _peer(MINE, "Vieux portable", online=False)
    machines = _by_id(machines_view.build(_snap(peers=[aside, gone])))
    assert machines[f"peer:{FRIEND}"]["state"] == "set_aside"
    assert machines[f"peer:{FRIEND}"]["attention"]
    assert machines[f"peer:{FRIEND}"]["actions"] == ["restore", "refresh", "options", "unpair"]
    assert machines[f"peer:{MINE}"]["state"] == "offline" and not machines[f"peer:{MINE}"]["online"]


# ----------------------------------------------------------------- réseau public
def test_a_public_node_is_never_named_and_shows_eight_characters_of_its_key_at_most():
    [node] = machines_view.build(_snap(public={"key": "Ma" * 26, "nodes": [
        _node(LENDER, models=["qwen3.5:9b"] * 3 + [f"m{i}" for i in range(40)])]}))
    assert node["kind"] == "public" and node["name"] == "" and node["node_id"] is None
    assert node["short"] == LENDER[:8] and node["id"] == f"public:{LENDER[:8]}"
    assert LENDER[:machines_view.SHORT + 1] not in json.dumps(node)
    assert node["public"] == {"short": LENDER[:8], "lends": True, "free": 1, "tps": None,
                              "images": [], "videos": []}
    assert node["models"][0] == "qwen3.5:9b" and node["models_n"] == 41      # sans doublon
    assert len(node["models"]) == machines_view.MAX_MODELS
    assert node["state"] == "free" and node["actions"] == []
    # Les modèles d'images qu'il prête aux inconnus (« image:… ») : à part, sans la marque
    [painter] = machines_view.build(_snap(public={"nodes": [
        _node(LENDER, models=["qwen3.5:9b", "image:z-image-turbo"])]}))
    assert painter["models"] == ["qwen3.5:9b"] and painter["public"]["images"] == ["z-image-turbo"]
    # Ses modèles de vidéos, sous la même marque : à part aussi, d'après le catalogue
    [filmmaker] = machines_view.build(_snap(public={"nodes": [
        _node(LENDER, models=["image:z-image-turbo", "image:wan2.1-t2v-1.3b", "image:inconnu"])]}))
    assert filmmaker["models"] == [] and filmmaker["public"]["videos"] == ["wan2.1-t2v-1.3b"]
    assert filmmaker["public"]["images"] == ["z-image-turbo", "inconnu"]
    # Deux clés au même début : une ligne chacune
    twins = machines_view.build(_snap(public={"nodes": [_node(LENDER), _node(LENDER[:8] + "Q" * 44)]}))
    assert len({m["id"] for m in twins}) == 2


def test_a_public_node_answering_my_conversation_is_busy_and_says_so():
    snap = _snap(public={"nodes": [_node(LENDER)]})
    with placement._occupy(public_chat.model_id(LENDER, "qwen3.5:9b"), None):
        asking = machines_view.public_asking()
        assert asking == {LENDER: {"qwen3.5:9b": 1}}
        [node] = machines_view.build(snap, asking=asking)
    assert node["state"] == "busy" and node["active"]
    assert node["doing"] == [{"what": "public_for_me", "n": 1, "model": "qwen3.5:9b"}]
    assert machines_view.public_asking() == {}


# --------------------------------------------------------------------------- tri
def test_waiting_then_active_then_public_mine_friends_local_then_offline():
    busy = _peer(FRIEND, "Ami occupé", trust="friend")
    busy["live"]["for_me"] = [{"title": "x", "units": 2}]
    snap = _snap(
        peers=[_peer(MINE, "Zèbre"), busy, _peer("d" * 32, "Absente", online=False)],
        visible=[{"node_id": OTHER, "name": "Tablette", "address": "192.168.1.30", "port": 1,
                  "last_seen": 1}],
        introductions=[{"intro": "i1", "machine": "Nouvelle", "presenter": "x", "created": 1}],
        public={"nodes": [_node(STRANGER, lends=False, free=None, seen_s=1),
                          _node(LENDER, free=0, seen_s=9), _node("Lf" * 26, free=1, seen_s=20)]})
    order = [m["id"] for m in machines_view.build(snap)]
    assert order == ["intro:i1", f"peer:{FRIEND}", "public:LfLfLfLf", "public:KqKqKqKq",
                     f"public:{STRANGER[:8]}", f"peer:{MINE}", f"local:{OTHER}", f"peer:{'d' * 32}"]


# ------------------------------------------------------------- rien de confidentiel
def test_nothing_confidential_leaves_the_list():
    hostile_pairing = {"node_id": OTHER, "name": "Tablette", "code": "123456", "ref": "r",
                       "key": "SECRET-DE-PAIRE", "local_ok": False}
    snap = _snap(peers=[_peer(MINE, "PC fixe", key="SECRET-DE-PAIRE")],
                 pairing=[hostile_pairing],
                 public={"key": "Ma" * 26, "nodes": [_node(LENDER), _node(MINE_PUBLIC)]})
    text = json.dumps(machines_view.build(snap, public_keys={MINE: MINE_PUBLIC}))
    for secret in ("SECRET-DE-PAIRE", NET_ID, LENDER, MINE_PUBLIC, "Ma" * 26):
        assert secret not in text


def test_the_interface_api_lists_the_machines_without_full_public_keys():
    auth = {"X-Aiodos-Token": "jeton"}
    with TestClient(create_app("jeton", {"testserver"})) as client:
        app = client.app
        while not hasattr(app, "state"):              # sous les gardes (api/security.py)
            app = app.app
        app.state.network.public._status = {"enabled": True, "key": "Ma" * 26,
                                            "nodes": [_node(LENDER)]}
        response = client.get("/api/network", headers=auth)
        body = response.json()
    assert [m["id"] for m in body["machines"]] == [f"public:{LENDER[:8]}"]
    assert "nodes" not in body["public"] and body["public"]["count"] == 1
    assert LENDER not in response.text


# ------------------------------------- la clé publique, dite à mes machines seulement
def test_my_machines_say_their_public_key_and_a_friend_never_hears_it():
    peers.save(MINE, "PC fixe", b"m" * 32, "127.0.0.1", 1)
    peers.save(FRIEND, "PC de Léa", b"f" * 32, "127.0.0.1", 2, trust=sharing.FRIEND)
    manager = SimpleNamespace(worker=None)
    assert "public_key" not in activity.field(manager, MINE)          # pas rejoint
    public._remember({"enabled": True, "key": MINE_PUBLIC, "nodes": []})
    assert activity.field(manager, MINE)["public_key"] == MINE_PUBLIC
    assert "public_key" not in activity.field(manager, FRIEND)
    assert profile.clean(activity.field(manager, MINE))["public_key"] == MINE_PUBLIC
    assert "public_key" not in profile.clean({"public_key": "<img src=x>"})
    # Reçue : gardée en mémoire ; absente des nouvelles suivantes : oubliée
    activity.note(MINE, {"public_key": LENDER})
    activity.note(FRIEND, {"public_key": "trop-court"})
    assert activity.public_keys() == {MINE: LENDER}
    activity.note(MINE, {})
    assert activity.public_keys() == {}
    activity.forget(MINE)
    activity.forget(FRIEND)


# ------------------------------------------------------------ la vue (Node.js)
HOSTILE = "<img src=x onerror=alert(1)>"

STUBS = {
    "api.js": "export const get = async () => ({}); export const post = async () => ({});\n"
              "export const del = async () => ({});\n",
    "ui.js": "export const modal = async () => ({ ok: false }); export const menu = () => {};\n"
             "export const toast = () => {}; export const errorToast = () => {};\n"
             "export const confirmDanger = async () => false;\n",
}


def _run_view(tmp_path, script: str, *args):
    js = tmp_path / "js"
    (js / "views").mkdir(parents=True)
    static = paths.STATIC_DIR / "js"
    for name in ("dom.js", "icons.js", "state.js", "fold.js", "views/connected.js",
                 "views/machine-row.js", "views/machine-live.js", "views/network-peers.js",
                 "views/network-pairing.js"):
        (js / name).write_bytes((static / name).read_bytes())
    for name, text in STUBS.items():
        (js / name).write_text(text, encoding="utf-8")
    prelude = i18n_js.install(js, module="./js/i18n.js")
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    (tmp_path / "run.js").write_text(prelude + script, encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "run.js"), *args], capture_output=True, text=True,
                          encoding="utf-8", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_the_list_escapes_what_comes_from_other_machines(tmp_path):
    """Un nom, un modèle, une version, une adresse ou un code viennent d'ailleurs : échappés.
    Une clé publique trop longue reçue malgré tout est coupée à 8 caractères."""
    peer = _peer(MINE, HOSTILE)
    peer["profile"]["version"] = HOSTILE
    peer["live"]["for_it"] = [HOSTILE]
    snap = _snap(peers=[peer], visible=[{"node_id": OTHER, "name": HOSTILE, "address": HOSTILE,
                                         "port": 1, "last_seen": 1}],
                 pairing=[{"node_id": OTHER, "name": HOSTILE, "code": HOSTILE, "ref": "r",
                           "address": HOSTILE, "local_ok": False}],
                 introductions=[{"intro": HOSTILE, "machine": HOSTILE, "presenter": HOSTILE,
                                 "created": 1}],
                 public={"nodes": [_node(LENDER, models=[HOSTILE, f"image:{HOSTILE}",
                                                         "image:wan2.1-t2v-1.3b"])]})
    machines = machines_view.build(snap, asking={LENDER: {HOSTILE: 1}})
    # Ce que dirait un nœud trompé : une « abréviation » longue et piégée, un état inconnu
    stranger = next(m for m in machines if m["kind"] == "public")
    machines.append(dict(stranger, id='x"><img src=x>', short="<b>" + LENDER, state="<i>",
                         version=HOSTILE, doing=[{"what": "<x>"}, {"what": "for_me", "n": HOSTILE}]))
    net = {"enabled": True, "machines": machines, "peers": [peer], "public": {"wanted": True}}
    html = _run_view(tmp_path, "import { listHtml } from './js/views/connected.js';\n"
                     "console.log(JSON.stringify(listHtml(JSON.parse(process.argv[2]), 'all', true)));\n",
                     json.dumps(net))
    assert "<img" not in html and "&lt;img" in html
    assert "<b>" not in html.replace("<b class=\"mx-name\">", "")
    assert "Inconnu KqKqKqKq…" in html and "Inconnu &lt;b&gt;KqKqK…" in html
    assert LENDER[:9] not in html
    assert "à moi" in html and "réseau public" in html and "réseau local" in html
    assert "en charge · batterie 84 %" in html and "qwen3.5:9b, gemma3:4b +1" in html
    assert "images : &lt;img" in html and "Modèles d&#39;images prêtés aux inconnus : &lt;img" in html
    assert "vidéos : wan2.1-t2v-1.3b" in html
    assert "Modèles de vidéos prêtés aux inconnus : wan2.1-t2v-1.3b" in html
    assert "Appairer" not in html                 # la machine détectée est en cours d'appairage
    assert 'data-act="check_code"' in html and 'data-act="answer_intro"' in html
    assert 'data-act="unpair"' in html and 'data-act="options"' in html


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_ten_lines_then_show_the_others_and_waiting_lines_under_any_filter(tmp_path):
    machines = [{"id": f"public:{i:08d}", "kind": "public", "short": f"{i:08d}", "online": True,
                 "state": "free", "attention": False, "public": {"lends": True, "free": 1}}
                for i in range(14)]
    machines.insert(0, {"id": f"local:{OTHER}", "kind": "local", "name": "Tablette", "online": True,
                        "state": "confirm", "attention": True, "actions": ["check_code"],
                        "pairing": {"code": "123456", "local_ok": False}})
    net = {"enabled": True, "machines": machines, "peers": [], "public": {"wanted": True}}
    result = _run_view(
        tmp_path,
        "import { listHtml, filterCounts, pick } from './js/views/connected.js';\n"
        "const net = JSON.parse(process.argv[2]);\n"
        "console.log(JSON.stringify({ page: listHtml(net, 'all', false), all: listHtml(net, 'all', true),\n"
        "  mine: listHtml(net, 'mine', false), counts: filterCounts(net.machines),\n"
        "  rows: pick(net.machines, 'public').rows.length }));\n",
        json.dumps(net))
    assert result["page"].count('<details class="mx') == 10 and "Voir les 5 autres" in result["page"]
    assert result["all"].count('<details class="mx') == 15 and "Voir moins" in result["all"]
    assert result["counts"] == {"all": 15, "public": 14, "mine": 0, "friend": 0, "local": 1}
    # « Mes machines » : rien, sauf l'appairage à confirmer, montré quel que soit le filtre
    assert result["mine"].count('<details class="mx') == 1 and "Tablette" in result["mine"]
    assert 'data-attn="1" open>' in result["mine"]   # sa ligne dépliée
    assert result["rows"] == 10
