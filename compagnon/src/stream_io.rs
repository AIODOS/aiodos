//! Un flux QUIC bidirectionnel vu comme une connexion (lecture + écriture), pour y faire
//! passer un échange HTTP/1.1 avec hyper.
//!
//! Fermeture : quand hyper termine proprement l'échange, il ferme l'écriture (FIN) ; si
//! le flux est abandonné en route (client parti, Stop d'une conversation…), il est
//! réinitialisé, et l'autre côté arrête aussitôt : sa propre écriture échoue, ce qui
//! coupe la requête relayée vers AIODOS, qui arrête le calcul.

use std::{
    io,
    pin::Pin,
    task::{Context, Poll},
};

use iroh::endpoint::{RecvStream, SendStream};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

use crate::pool::Activity;

/// Code de réinitialisation d'un flux abandonné.
const CANCELLED: u32 = 1;

pub struct QuicIo {
    send: SendStream,
    recv: RecvStream,
    finished: bool,
    _activity: Activity,
}

impl QuicIo {
    pub fn new(send: SendStream, recv: RecvStream, activity: Activity) -> Self {
        QuicIo { send, recv, finished: false, _activity: activity }
    }
}

impl AsyncRead for QuicIo {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        AsyncRead::poll_read(Pin::new(&mut self.recv), cx, buf)
    }
}

impl AsyncWrite for QuicIo {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        AsyncWrite::poll_write(Pin::new(&mut self.send), cx, buf)
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        AsyncWrite::poll_flush(Pin::new(&mut self.send), cx)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let result = AsyncWrite::poll_shutdown(Pin::new(&mut self.send), cx);
        if let Poll::Ready(Ok(())) = result {
            self.finished = true;
        }
        result
    }
}

impl Drop for QuicIo {
    fn drop(&mut self) {
        if !self.finished {
            let _ = self.send.reset(CANCELLED.into());
        }
    }
}
