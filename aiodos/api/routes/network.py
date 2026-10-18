"""Réseau local vu par l'interface : activation, machines, appairage."""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aiodos import settings
from aiodos.network import consult, group, invitations, peers, sharing
from aiodos.network.client import PeerError

router = APIRouter(prefix="/api/network")


class EnableIn(BaseModel):
    enabled: bool


class AddressIn(BaseModel):
    address: str


class NodeIn(BaseModel):
    node_id: str


class AnswerIn(BaseModel):
    node_id: str
    accept: bool
    trust: str = sharing.MINE           # l'autre machine : une des miennes, ou un ami


class TrustIn(BaseModel):
    trust: str


class CodeIn(BaseModel):
    code: str


class IntroAnswerIn(BaseModel):
    intro: str
    accept: bool
    always: bool = False                # les prochaines machines du même groupe aussi


def _manager(request: Request):
    return request.app.state.network


@router.get("")
def status(request: Request):
    return _manager(request).snapshot()


@router.post("/enable")
async def enable(body: EnableIn, request: Request):
    manager = _manager(request)
    await asyncio.to_thread(settings.set_value, "network_enabled", body.enabled)
    await manager.apply()
    return manager.snapshot()


@router.post("/public")
async def public_enable(body: EnableIn, request: Request):
    """« Rejoindre le réseau public » (phase 3.0, désactivé par défaut) : un annuaire, rien
    de confié. Il passe par le compagnon Internet."""
    manager = _manager(request)
    if body.enabled and not manager.internet.enabled:
        raise HTTPException(400, "Active d'abord « Internet » : le réseau public passe par lui.")
    await asyncio.to_thread(settings.set_value, "public_network", body.enabled)
    await manager.public.sync()
    manager.changed()
    return manager.snapshot()


@router.get("/internet")
async def internet_status(request: Request):
    """Compagnon Internet (aiodos-net) : état lu à l'instant, connexions comprises."""
    return await _manager(request).internet.live_status()


@router.post("/internet")
async def internet_enable(body: EnableIn, request: Request):
    manager = _manager(request)
    await asyncio.to_thread(settings.set_value, "internet_enabled", body.enabled)
    await manager.internet.apply()
    manager.changed()
    return await manager.internet.live_status()


@router.post("/add")
async def add(body: AddressIn, request: Request):
    try:
        return await _manager(request).add_manual(body.address)
    except (PeerError, ValueError) as exc:
        raise HTTPException(400, f"Impossible de joindre {body.address} : {exc}")


@router.post("/pair")
async def pair(body: NodeIn, request: Request):
    manager = _manager(request)
    target = manager.visible.get(body.node_id)
    if target is None:
        raise HTTPException(404, "Machine introuvable sur le réseau.")
    try:
        pending = await asyncio.to_thread(manager.pairing.start, target["address"], target["port"])
    except PeerError as exc:
        raise HTTPException(400, str(exc))
    return {"node_id": pending.node_id, "name": pending.name, "code": pending.code}


@router.post("/pair/answer")
async def answer(body: AnswerIn, request: Request):
    try:
        await asyncio.to_thread(_manager(request).pairing.answer, body.node_id, body.accept,
                                body.trust)
    except PeerError as exc:
        raise HTTPException(400, str(exc))
    return _manager(request).snapshot()


# ------------------------------------------- invitation à distance (étape 2.4)
@router.post("/invitations")
async def invite(request: Request):
    """1. Invitation à envoyer à un ami : {id, code, expires}."""
    manager = _manager(request)
    try:
        created = await asyncio.to_thread(invitations.create, manager)
    except invitations.InvitationError as exc:
        raise HTTPException(400, str(exc))
    manager.changed()
    return created


@router.post("/invitations/accept")
async def accept_invitation(body: CodeIn, request: Request):
    """2. Invitation reçue d'un ami : {reply : la réponse à lui renvoyer, name}."""
    manager = _manager(request)
    try:
        answer = await asyncio.to_thread(invitations.accept, manager, body.code)
    except invitations.InvitationError as exc:
        raise HTTPException(400, str(exc))
    await manager.internet.sync()             # l'invitant admis par le compagnon
    manager.changed()
    return answer


@router.post("/invitations/{invitation_id}/reply")
async def invitation_reply(invitation_id: int, body: CodeIn, request: Request):
    """3. Réponse de l'ami collée : il est admis, et l'appairage commence par Internet
    (comme /pair : {node_id, name, code})."""
    manager = _manager(request)
    try:
        target = await asyncio.to_thread(invitations.answered, manager, invitation_id, body.code)
    except invitations.InvitationError as exc:
        raise HTTPException(400, str(exc))
    await manager.internet.sync()             # avant de composer sa clé
    try:
        pending = await asyncio.to_thread(manager.pairing.start_remote, target)
    except PeerError as exc:
        raise HTTPException(400, f"« {target['name']} » injoignable par Internet : {exc}")
    return {"node_id": pending.node_id, "name": pending.name, "code": pending.code}


@router.delete("/invitations/{invitation_id}")
async def cancel_invitation(invitation_id: int, request: Request):
    manager = _manager(request)
    await asyncio.to_thread(invitations.cancel, invitation_id)
    await manager.internet.sync()
    manager.changed()
    return manager.snapshot()


@router.post("/peers/{node_id}/refresh")
async def refresh(node_id: str, request: Request):
    await _manager(request).refresh_now(node_id)
    return _manager(request).snapshot()


@router.post("/peers/{node_id}/trust")
async def set_trust(node_id: str, body: TrustIn, request: Request):
    """Une de mes machines, ou celle d'un ami (cf. network/sharing.py)."""
    if body.trust not in sharing.LEVELS:
        raise HTTPException(400, f"Niveau de confiance inconnu : {body.trust!r}")
    before = await asyncio.to_thread(peers.trust_of, node_id)
    if not await asyncio.to_thread(peers.set_trust, node_id, body.trust):
        raise HTTPException(404, "Machine appairée introuvable.")
    manager = _manager(request)
    if body.trust == sharing.FRIEND and before != sharing.FRIEND:
        manager.present_later(node_id)    # un ami rencontre mes autres machines
    elif body.trust == sharing.MINE and before != sharing.MINE:
        # une de mes machines rencontre mes amis
        await asyncio.to_thread(manager.present_new_machine, node_id)
    manager.changed()
    return manager.snapshot()


@router.post("/peers/{node_id}/present")
async def present(node_id: str, request: Request):
    """Présente à la machine d'un ami mes autres machines (appairage par groupe).
    → {"introduced", "asked", "known", "unreachable", "refused"} : des noms."""
    try:
        report = await asyncio.to_thread(_manager(request).group.present, node_id)
    except PeerError as exc:
        raise HTTPException(400, str(exc))
    _manager(request).changed()
    return report


@router.post("/introductions/answer")
async def answer_introduction(body: IntroAnswerIn, request: Request):
    """Réponse à une présentation reçue (« toujours » : accepter d'office les prochaines
    machines du même groupe)."""
    try:
        await asyncio.to_thread(_manager(request).group.answer, body.intro, body.accept,
                                body.always)
    except PeerError as exc:
        raise HTTPException(400, str(exc))
    return _manager(request).snapshot()


@router.post("/peers/{node_id}/restore")
async def restore(node_id: str, request: Request):
    """Rétablit une machine mise de côté après un contrôle échoué."""
    await asyncio.to_thread(peers.set_aside, node_id, None)
    _manager(request).changed()
    return _manager(request).snapshot()


@router.get("/peers/{node_id}/shared")
def shared(node_id: str):
    """Journal de ce qui est parti chez cette machine (fichiers et pages confiés)."""
    return {"entries": sharing.sent_log(node_id)}


@router.get("/peers/{node_id}/consulted")
def consulted(node_id: str):
    """Journal des recherches de cette machine dans mes bibliothèques ouvertes."""
    return {"entries": consult.entries(node_id)}


@router.delete("/peers/{node_id}")
async def unpair(node_id: str, request: Request, only_here: bool = False,
                 with_introduced: bool = False):
    """Retire une machine appairée ; aucune présentation ne la ramène ici (un appairage par
    code, si). Celle d'un ami l'est aussi de toutes mes machines (décision de l'auteur),
    sauf only_here : seulement de celle-ci. Une de mes machines retirée (perdue, volée) :
    with_introduced retire aussi, partout, les machines d'amis qu'elle a présentées ici.
    → l'état, et « missed » : mes machines à prévenir plus tard (hors ligne)."""
    manager = _manager(request)
    trust = await asyncio.to_thread(peers.trust_of, node_id)
    targets = [node_id]
    if trust == sharing.MINE and with_introduced:
        targets = await asyncio.to_thread(group.introduced_here_by, node_id) + targets
    missed = set()
    for target in targets:
        friend = await asyncio.to_thread(peers.trust_of, target) == sharing.FRIEND
        await asyncio.to_thread(group.decline, target, "retirée ici")
        await manager.unpair(target)
        if friend and not (only_here and target == node_id):
            missed.update(await asyncio.to_thread(manager.group.spread, "group/remove",
                                                  {"node_id": target}))
    return {**manager.snapshot(), "missed": sorted(missed)}
