//! Corps et réponses HTTP communs : réponses JSON du compagnon lui-même, et corps
//! relayés tels quels (une réponse en flux passe morceau par morceau).

use bytes::Bytes;
use http_body_util::{BodyExt, Full, combinators::UnsyncBoxBody};
use hyper::{Response, StatusCode, header};
use serde_json::{Value, json};

pub type BoxError = Box<dyn std::error::Error + Send + Sync>;

/// Corps de requête ou de réponse, d'où qu'il vienne.
pub type Body = UnsyncBoxBody<Bytes, BoxError>;

/// Enveloppe un corps quelconque (corps reçu, texte…) dans le type commun.
pub fn boxed<B>(body: B) -> Body
where
    B: hyper::body::Body<Data = Bytes> + Send + 'static,
    B::Error: Into<BoxError>,
{
    body.map_err(Into::into).boxed_unsync()
}

pub fn empty() -> Body {
    boxed(Full::new(Bytes::new()))
}

/// Réponse JSON.
pub fn json(status: StatusCode, value: Value) -> Response<Body> {
    let mut response = Response::new(boxed(Full::new(Bytes::from(value.to_string()))));
    *response.status_mut() = status;
    response
        .headers_mut()
        .insert(header::CONTENT_TYPE, header::HeaderValue::from_static("application/json"));
    response
}

/// Refus ou panne expliqués : `{"detail": "…"}`, comme les erreurs de FastAPI, pour que
/// le client Python d'AIODOS affiche le message tel quel.
pub fn detail(status: StatusCode, message: impl Into<String>) -> Response<Body> {
    json(status, json!({ "detail": message.into() }))
}

/// En-têtes propres à un seul tronçon de connexion : jamais relayés d'un côté à l'autre
/// (hyper refait le découpage du corps lui-même).
pub const HOP_BY_HOP: &[&str] = &[
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];

pub fn strip_hop_by_hop(headers: &mut header::HeaderMap) {
    for name in HOP_BY_HOP {
        headers.remove(*name);
    }
}
