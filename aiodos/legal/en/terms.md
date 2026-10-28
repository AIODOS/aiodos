# AIODOS Terms of Use

*Version 3, October 28, 2026*

## 1. Purpose

These terms govern the use of AIODOS: the Windows program, the Android app, their updates,
and the network formed by the machines that use them. By installing or using AIODOS, you
accept these terms and the charter. If you do not accept them, do not install AIODOS, or
uninstall it.

## 2. Publisher and license

AIODOS is published by 22eme Arkane and the project's contributors. It is free and open
software, distributed under the Apache License, version 2.0 (LICENSE file, installed with
AIODOS). This license governs your rights to the code: to use, copy, modify and redistribute
it. These terms and the charter govern the use of the software and of the network; they do
not take away any of the rights the license gives you to the code. Where the code is
concerned, the license prevails.

## 3. No account, no central server

AIODOS requires no account and no subscription. The publisher runs no server that would
receive your conversations, documents or creations, and AIODOS sends no usage statistics.
Your data is stored on your devices, in the AIODOS folder of your profile; backing it up is
up to you.

## 4. Third-party services

Depending on your settings, AIODOS contacts services that do not belong to the publisher and
have their own terms:

- **Internet relays** (by default, n0's public relays, or the one you choose) and
  **directories** (n0's DNS, the Mainline DHT), to connect your machines to those of your
  friends and of the public network. A relay only sees encrypted traffic, but it sees which
  machines talk to each other, from which IP addresses, and how much;
- a **search engine** (DuckDuckGo) and the pages found, when web search is enabled and you
  approve a search;
- the sites that **models and engines** come from (Hugging Face, Ollama…), only when you ask
  for a download.

## 5. Sharing power

- Part of your requests can be computed by other machines: yours, your friends', those of
  the public network. They travel encrypted, but the machine that computes them has to read
  them to process them. AIODOS is designed to keep nothing on the machine that computes; the
  publisher cannot guarantee that another person's program has not been modified.
- From the first launch, and until you turn it off: your machine joins the public network's
  directory (it announces its AIODOS version, the list of its models, whether it lends, its
  free places and its speed there, nothing personal), your conversations can be computed by
  strangers and your machine computes theirs (see below), your graphics card creates the
  images that the friends you have added ask of it, and the applications on this machine can
  ask you for access to AIODOS (each one has to be accepted by you).
- **Your conversations on a stranger's machine** ("Have my conversations computed by
  strangers"): a stranger's machine on the public network can compute your conversations.
  It reads the conversation (your text, the system prompt), but doesn't know who is asking:
  each conversation has its own throwaway key, and the request goes through a relay. AIODOS
  keeps nothing there. Your text itself can still reveal who wrote it (a name, an address in
  your question). To keep a conversation on your own machines, turn this setting off, or
  turn on "On my machines only" in that conversation. Having a larger model on a public
  network machine answer, or sending a stranger the passages of your documents (your
  libraries, the memory of your conversations), waits for your consent, asked once and
  changeable in Settings. Your attachments, images and files never go to a stranger.
- **Strangers' conversations on your machine** ("Lend my computing power to strangers'
  conversations"): your machine computes them within the limit of the "Power lent" (60% by
  default), never on battery, a phone only while charging and on Wi-Fi, and after your own
  work, your machines and your friends. It keeps nothing, neither the conversation nor who
  is asking, and does not show it to you: you don't choose and you don't see what it
  computes. A stranger could therefore ask it for an illegal text: your machine would only
  run a public model on it, without keeping any of it. Everyone remains responsible for
  what they ask (part 6). If this doesn't suit you, turn this lending off in Settings.
- Lending your power uses your processor, graphics card, memory, battery and connection:
  electricity, data, heat, wear. AIODOS keeps this lending gentle (low priority, not on
  battery, phone only while charging) and you can stop it at any time in Settings.
- Lending is voluntary. The exchange counts AIODOS shows have no monetary value.

## 6. Created content

- The answers, images and videos of the models can be false, incomplete, biased or
  shocking. They do not replace the advice of a professional (doctor, lawyer, financial
  adviser…): check before relying on them.
- An answer computed by a stranger's machine can be false, by mistake or because that
  machine cheats. It is marked "public network" in the conversation; random checks compare
  it with that of another machine, and a machine that cheats is set aside. These checks only
  cover some of the answers.
- You alone are responsible for what you ask, including of other people's machines, what you
  create and what you do with it, including sharing it.
- The publisher claims no rights to what you create with AIODOS. The rights to a creation
  depend on the applicable law and on the license of the model used.

## 7. Third-party models and software

AI models are not provided by the publisher: others publish them, under their own licenses,
some of which restrict commercial use or certain uses. AIODOS shows a model's license when
it is restrictive and asks you to confirm it; it is up to you to respect it. The same goes
for the engines AIODOS can use (llama.cpp, stable-diffusion.cpp, Ollama, ComfyUI…).

## 8. Updates

AIODOS can update itself; this can be turned off in Settings. Each version is signed by the
publisher and checked before being installed; it may reach you through your paired machines.

## 9. Minors

If you are under 15, or under the age set by the law of your country to use such a service
on your own, use AIODOS with the consent and under the responsibility of a parent or legal
guardian.

## 10. No warranty

AIODOS is provided "as is", without warranty of any kind, as its license states. It is
software under development: it may contain errors, lose data or stop working. The network
depends on everyone's machines and on third-party services: it may be unavailable. Keep
backups of what matters.

## 11. Liability

To the extent permitted by law, the publisher and the contributors are not liable for
damages resulting from the use of AIODOS or the inability to use it, from created content,
from the behavior of other participants or from third-party services. These terms limit
neither a liability that the law forbids limiting, nor the rights the law gives you as a
consumer. You are answerable for your use of AIODOS, including what the applications you
connect to it do.

## 12. Stopping

You can stop using AIODOS at any time: uninstall it (keeping your data or deleting it),
leave the public network, remove your friends. Other participants can also stop working
with your machine, in particular if you do not respect the charter.

## 13. Changes

These terms and the charter may change. Each new version has a number and a date; AIODOS
shows it to you and asks you to accept it to keep using it.

## 14. Governing law

These terms are governed by French law. In the event of a dispute, an amicable solution is
sought first; failing that, the French courts have jurisdiction, subject to the rules that
protect you in your country if you are a consumer. The French version of these texts
prevails.

## 15. Contact

Questions, reports, security flaws: the project team, on its repository:
github.com/AIODOS/aiodos.
