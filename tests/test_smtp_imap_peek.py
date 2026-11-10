"""Reading replies over IMAP leaves the mailbox's unread flags alone."""

from email.message import EmailMessage

from sellbird.config import EnvConfig
from sellbird.integrations import smtp_mail


class FakeImap:
    instances = []

    def __init__(self, host, port):
        self.fetched = []
        self.readonly = None
        FakeImap.instances.append(self)

    def login(self, user, password):
        return "OK", [b""]

    def select(self, box, readonly=False):
        self.readonly = readonly
        return "OK", [b"1"]

    def search(self, charset, criterion):
        assert criterion == "UNSEEN"
        return "OK", [b"7"]

    def fetch(self, msg_id, spec):
        self.fetched.append(spec)
        msg = EmailMessage()
        msg["From"] = "Owner <owner@gym.example>"
        msg["Subject"] = "Re: pregunta rápida"
        msg["Message-ID"] = "<reply-1@gym.example>"
        msg.set_content("Me interesa, ¿cuándo platicamos?")
        return "OK", [(b"7 (BODY[] {100}", msg.as_bytes())]

    def logout(self):
        return "BYE", [b""]


def test_unseen_replies_are_peeked_not_marked_read(monkeypatch):
    FakeImap.instances.clear()
    monkeypatch.setattr(smtp_mail.imaplib, "IMAP4_SSL", FakeImap)
    env = EnvConfig(smtp_host="smtp.example.com", smtp_username="me@example.com",
                    smtp_password="pw")
    provider = smtp_mail.SmtpImapProvider(None, env, identity=object())
    out = provider._fetch_unseen(10)
    assert [m.from_email for m in out] == ["owner@gym.example"]
    assert FakeImap.instances[0].fetched == ["(BODY.PEEK[])"]
