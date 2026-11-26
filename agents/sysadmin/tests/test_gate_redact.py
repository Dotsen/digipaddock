import io
import time

import pytest

from gate import agent_ro_gate as gate

MARK = "скрыто секретов"


def red(text: str) -> str:
    return gate.redact(text.encode("utf-8")).decode("utf-8")


def count(text: str) -> int:
    return gate.redact_counted(text.encode("utf-8"))[1]


# --- redaction -----------------------------------------------------------------

def test_proxy_url_in_log_line():
    line = "2026-10-05 10:00:01 INFO parser_proxy proxy=socks5://fakeuser:fakepass123@proxy.provider.example:5000 ok"
    out = red(line)
    assert "fakeuser" not in out and "fakepass123" not in out
    assert "socks5://***:***@proxy.provider.example:5000" in out


@pytest.mark.parametrize("url,expected", [
    ("mysql://dbu:dbpw@127.0.0.1/shop", "mysql://***:***@127.0.0.1/shop"),
    ("https://u:p@example.org/path?x=1", "https://***:***@example.org/path?x=1"),
    ("socks5://u:p@:1080", "socks5://***:***@:1080"),
])
def test_url_credentials_keep_scheme_and_host(url, expected):
    assert red("conn " + url + " end") == "conn " + expected + " end"


def test_url_without_password_is_kept():
    assert red("git clone ssh://git@github.com/org/repo.git") == "git clone ssh://git@github.com/org/repo.git"


def test_nginx_access_line_query_secrets():
    line = ('198.51.100.2 - - [05/Oct/2026:10:00:00 +0000] "GET /api/v1/orders?token=FAKETOKEN123&sig=FAKESIG456'
            '&page=2 HTTP/1.1" 200 512 "-" "curl/7.0"')
    out = red(line)
    assert "FAKETOKEN123" not in out and "FAKESIG456" not in out
    assert "?token=***&sig=***&page=2 HTTP/1.1" in out
    assert out.startswith("198.51.100.2 - - [05/Oct/2026:10:00:00 +0000]")


def test_gost_argv_both_urls():
    out = red("gost -L socks5://u:p@:1080 -F http://u2:p2@h:8080")
    assert out == "gost -L socks5://***:***@:1080 -F http://***:***@h:8080"


@pytest.mark.parametrize("line,hidden", [
    ('{"password": "fakepw1", "user": "bob"}', "fakepw1"),
    ('{"token":"faketok2","a":1}', "faketok2"),
    ("password: fakepw3", "fakepw3"),
    ("'api_key' => 'fakekey4',", "fakekey4"),
    ("DB_PASSWORD=fakepw5", "fakepw5"),
    ("password=fakepw6 user=bob", "fakepw6"),
    ("PrivateKey = fakeprivkey7=", "fakeprivkey7"),
    ("PresharedKey = fakepsk8=", "fakepsk8"),
    ("curl --password=fakepw9 http://h/", "fakepw9"),
    ("x_api_key=fakekey10", "fakekey10"),
    ('{"key": "fakekey11"}', "fakekey11"),
    ("client_secret: fakesecret12", "fakesecret12"),
    ('password="fake pw 13"', "fake pw 13"),
    ('"password": "unterminated14', "unterminated14"),
    ("AWS_SECRET_ACCESS_KEY=fakeaws15", "fakeaws15"),
])
def test_key_value_secrets_masked(line, hidden):
    out = red(line)
    assert hidden not in out
    assert "***" in out


def test_key_value_keeps_neighbours():
    assert red("a=1&password=fake&b=2") == "a=1&password=***&b=2"
    assert red("password=fake;next") == "password=***;next"
    assert red('{"password": "fake", "user": "bob"}') == '{"password": "***", "user": "bob"}'
    assert red("user=bob password=fake") == "user=bob password=***"


def test_mysql_dash_p():
    out = red("mysqldump -u root -pFAKESECRET16 shop")
    assert "FAKESECRET16" not in out and out.startswith("mysqldump -u root -p")
    assert red("ps -p 123") == "ps -p 123"


@pytest.mark.parametrize("line,expected", [
    ("           ├─1234 /usr/local/bin/proxyd -u proxyuser -p s3cr3t-Pass -l 0.0.0.0:1080", "-p ***"),
    ("ExecStart=/usr/bin/app --password hunter2 --db shop", "--password ***"),
    ("ExecStart=/usr/bin/app --passwd hunter2", "--passwd ***"),
    ("root  812 0.0 0.1 app --pass hunter2", "--pass ***"),
])
def test_dash_p_with_a_space_in_command_lines(line, expected):
    out = red(line)
    assert expected in out and "hunter2" not in out and "s3cr3t" not in out
    assert count(line) == 1


@pytest.mark.parametrize("line", [
    "ssh -p 22 host", "ps -p 1234 -o cmd", "mysql -p", "app -p -v", "app -p\n", "tar -xpf a.tar",
    "--no-p x", "/srv/x-p y",
])
def test_dash_p_ports_pids_and_bare_flags_are_kept(line):
    # «ip -p link» (pretty-print) is masked too: a word after -p is a password unless it is a port or a pid.
    assert red(line) == line


def test_php_define_and_dash_u_credentials():
    """Global review 15.10.2026: define('DB_PASSWORD', 'x') and curl -u user:pass were not masked."""
    out = red("define('DB_PASSWORD', 'fakepw17');\ndefine('DB_USER', 'shop');")
    assert out == "define('DB_PASSWORD', '***');\ndefine('DB_USER', 'shop');"
    assert red('define("API_KEY", "fakekey18")') == 'define("API_KEY", "***")'
    assert red("curl -u bob:fakepw19 https://h/") == "curl -u bob:*** https://h/"
    assert red("ExecStart=/usr/bin/x --user bob:fakepw20 -l 0.0.0.0:1080") == "ExecStart=/usr/bin/x --user bob:*** -l 0.0.0.0:1080"
    assert red("--user=bob:fakepw21") == "--user=bob:***" and count("-u a:b --user=c:d") == 2
    for line in ("ps -u root", "id -u", "useradd -u 1000 bob", "sudo -u digi ls", "Enter password, then press Enter",
                 "token, 5 more"):
        assert red(line) == line


def test_bearer_and_basic():
    assert red("Authorization: Bearer abcdef0123456789") == "Authorization: Bearer ***"
    assert red("authorization: bearer abcdef0123456789.xyz") == "authorization: bearer ***"
    assert red("Authorization: Basic ZmFrZTpmYWtl") == "Authorization: Basic ***"
    assert red("Bearer short") == "Bearer short"


def test_telegram_token():
    token = "123456789:" + "A" * 35
    assert red("sent via " + token + " ok") == "sent via *** ok"
    assert red("https://api.telegram.org/bot" + token + "/sendMessage") == "https://api.telegram.org/bot***/sendMessage"


def test_pem_block_with_end():
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nZmFrZWZha2U=\nZmFrZWZha2U=\n-----END OPENSSH PRIVATE KEY-----"  # privacy-scan: allow (fake sample)
    out = red("before\n" + pem + "\nafter\n")
    assert out == "before\n-----BEGIN PRIVATE KEY-----***-----END PRIVATE KEY-----\nafter\n"


def test_pem_block_truncated_masks_to_end():
    out = red("x\n-----BEGIN RSA PRIVATE KEY-----\nZmFrZWZha2U=\nZmFrZWZha2U=")  # privacy-scan: allow (fake sample)
    assert out == "x\n-----BEGIN PRIVATE KEY-----***-----END PRIVATE KEY-----"


def test_pem_inside_json_counted_once():
    text = '{"private_key": "-----BEGIN PRIVATE KEY-----\\nZmFrZQ==\\n-----END PRIVATE KEY-----\\n"}'  # privacy-scan: allow (fake sample)
    assert "ZmFrZQ" not in red(text)
    assert count(text) == 1


# --- ordinary text is left alone ---------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Oct  5 10:00:00 host sshd[1]: Failed password for root from 198.51.100.2 port 22 ssh2",
    "Accepted password for bob from 198.51.100.2",
    "PasswordAuthentication yes",
    "PermitEmptyPasswords no",
    "monkey=banana",
    "password authentication failed for user bob",
    "Password: ",
    "Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1        40G   12G   26G  32% /\n",
    "interface: wg0\n  public key: 7ZmFrZWZha2VmYWtlZmFrZWZha2VmYWtlZmFrZWZha2U=\n  private key: (hidden)\n"
    "  listening port: 51820\n\npeer: abcdefgh=\n  preshared key: (hidden)\n  endpoint: 198.51.100.2:51820\n",
    "2: eth0: <BROADCAST,MULTICAST,UP> mtu 1500 qdisc fq_codel state UP\n    inet 10.0.0.2/24 brd 10.0.0.255",
    "* nginx.service - A high performance web server\n   Active: active (running) since Mon 2026-10-05",
    "key rotation done; tokens issued: 5",
])
def test_ordinary_text_unchanged(text):
    data = text.encode("utf-8")
    assert gate.redact_counted(data) == (data, 0)


def test_non_utf8_bytes_survive():
    data = b"\xff\xfe\x80 raw \xc3 password=fake1 \xe0\x80 tail\n"
    out, n = gate.redact_counted(data)
    assert n == 1
    assert out == b"\xff\xfe\x80 raw \xc3 password=*** \xe0\x80 tail\n"


def test_count_is_number_of_masked_places():
    assert count("password=a token=b https://u:p@h/") == 3
    assert count("nothing here") == 0


def test_performance_one_megabyte():
    line = b"2026-10-05 10:00:01 INFO worker processed id=42 status=ok in 12ms path=/var/www/x token=abc&k=1\n"
    data = line * (1024 * 1024 // len(line))
    start = time.monotonic()
    out, n = gate.redact_counted(data)
    elapsed = time.monotonic() - start
    assert n == data.count(b"\n")
    assert elapsed < 1.0, elapsed


# --- main() --------------------------------------------------------------------------

def _run_main(monkeypatch, output: bytes, **flags):
    result = gate.Result(flags.get("code", 0), output, flags.get("truncated", False), flags.get("timed_out", False))
    monkeypatch.setattr(gate, "materialize", lambda plan: ["fake"])
    monkeypatch.setattr(gate, "run_limited", lambda argv, **kw: result)
    out, err = io.BytesIO(), io.BytesIO()
    code = gate.main([], {"SSH_ORIGINAL_COMMAND": "uptime"}, out, err)
    return code, out.getvalue().decode("utf-8", "replace")


def test_main_masks_output_and_appends_count_line(monkeypatch):
    code, out = _run_main(monkeypatch, b"proxy socks5://fakeu:fakep@h:1 ok\npassword=fakepw\n")
    assert code == 0
    assert "fakeu" not in out and "fakep@" not in out and "fakepw" not in out
    assert out.endswith("\n[agent-ro-gate: " + MARK + ": 2]\n")


def test_main_no_count_line_when_nothing_masked(monkeypatch):
    code, out = _run_main(monkeypatch, b"up 3 days\n")
    assert code == 0
    assert out == "up 3 days\n"


def test_main_count_line_goes_before_truncation_and_timeout_notes(monkeypatch):
    _, out = _run_main(monkeypatch, b"token=fake\n", truncated=True)
    assert out.index(MARK) < out.index("вывод обрезан")
    code, out = _run_main(monkeypatch, b"token=fake\n", timed_out=True)
    assert code == 124
    assert out.index(MARK) < out.index("таймаут")


# --- round 1: linearity, the edges of the output, head/tail without -c --------------------------------

PROXY_LINE = ("2026-10-05 10:00:01 INFO parser_proxy proxy=socks5://package-123-sessionid-abc:FakePassW0rd@proxy.provider.example:5000"
        " ok\n").encode()


@pytest.mark.parametrize("raw", [
    "head -c 100 /etc/hosts", "tail -c 100 /var/log/syslog", "tail -c +5 /var/log/syslog",
    "head --bytes=10 /etc/hosts", "tail --bytes 10 /var/log/syslog", "head -qc5 /etc/hosts",
])
def test_head_tail_byte_slicing_denied(raw):
    with pytest.raises(gate.Deny):
        gate.check(raw)


@pytest.mark.parametrize("raw", ["head -n 5 /etc/hosts", "tail -n +5 /var/log/syslog", "tail --lines=5 /var/log/syslog"])
def test_head_tail_line_slicing_allowed(raw):
    assert gate.check(raw).argv[0] in ("head", "tail")


@pytest.mark.parametrize("raw", [
    "cat -A /etc/app.conf", "cat -T /etc/app.conf", "cat -t /etc/app.conf", "cat -v /etc/app.conf",
    "cat -e /etc/app.conf", "cat -nA /etc/app.conf", "cat -vET /etc/app.conf",
])
def test_cat_that_reshapes_the_text_is_denied(raw):
    # review 26.11.2026: "password<Tab>= x" shown as "password^I= x", or "Р" as "M-PM- ", is no longer a pair the
    # masks recognise
    with pytest.raises(gate.Deny, match="маск"):
        gate.check(raw)
    assert red("password\t=\thunter2") == "password\t=\t***"
    assert "hunter2" in red("password^I=^Ihunter2")  # what cat -T printed: the mask cannot see it


@pytest.mark.parametrize("raw", ["cat /etc/hosts", "cat -n /etc/hosts", "cat -E /etc/hosts", "cat -bsu /etc/hosts"])
def test_cat_that_keeps_the_text_is_allowed(raw):
    assert gate.check(raw).argv[0] == "cat"


def test_truncated_proxy_line_no_password_prefix_leaks():
    # the output is cut only at the end (LIMIT 1 MB): head/tail without -c, the beginning is always a whole line
    password = b"FakePassW0rd"
    for k in range(len(PROXY_LINE)):
        out = gate.redact(PROXY_LINE[:k])
        for n in range(4, len(password) + 1):
            assert password[:n] not in out and password[-n:] not in out, (k, PROXY_LINE[:k])


def test_output_starting_with_template_unit_is_untouched():
    # systemctl list-units --failed gave app-agent@sysadmin.service as its first line — not a fragment of a URL
    # (the factory host, 05.10.2026)
    data = b"app-agent@sysadmin.service loaded failed failed DigiPaddock\n"
    assert gate.redact_counted(data) == (data, 0)


def test_edge_rules_count_and_marks():
    out, n = gate.redact_counted(b"line one\nproxy=socks5://user:secre")
    assert out == b"line one\nproxy=socks5://***" and n == 1
    assert gate.redact_counted(b"see https://example.com/x\n") == (b"see https://example.com/x\n", 0)


def test_edge_start_needs_a_token_before_at():
    # mysql prints the header "@@SESSION.…" first — that is not a fragment of a URL (example-prod, 05.10.2026)
    data = b"@@SESSION.max_execution_time\tCOUNT(*)\n30000\t22\n"
    assert gate.redact_counted(data) == (data, 0)


def test_pgp_private_key_block_masked():
    out = red("-----BEGIN PGP PRIVATE KEY BLOCK-----\nZmFrZQ==\n-----END PGP PRIVATE KEY BLOCK-----\nok")
    assert "ZmFrZQ" not in out and out.endswith("\nok")


def test_nul_stops_unquoted_value():
    assert gate.redact(b"--password=fake\x00--next\x00") == b"--password=***\x00--next\x00"


def _timed(data: bytes) -> float:
    start = time.monotonic()
    gate.redact_counted(data)
    return time.monotonic() - start


@pytest.mark.parametrize("name,data", ids=lambda v: v if isinstance(v, str) else "", argvalues=[
    ("alnum_run", b"a" * (1024 * 1024)),
    ("hex_4k_lines", (b"0123456789abcdef" * 256 + b"\n") * 256),
    ("nginx_8k_lines", (b"198.51.100.2 GET /?" + b"a" * 8000 + b" HTTP/1.1\n") * 128),
    ("mysql_words", (b"mysql " * 1365 + b"\n") * 128),
    ("mysql_one_line", b"mysql " * 170000),
    ("colon_chain", b"a:" * 524288),
    ("scheme_chain", b"a://b:" * 170000),
    ("begin_run", b"-----BEGIN " + b"A" * (1024 * 1024)),
    ("slashes_scheme", b"a://b:" + b"c" * (1024 * 1024)),
    ("env_backslashes", b"Environment=A=" + b"\\" * (1024 * 1024)),
    ("env_continued", b"Environment=" + b'"A=\\\n' * 200000),
])
def test_adversarial_input_is_linear(name, data):
    assert _timed(data) < 2.0, name


# --- security monitoring: the snapshot and auth.log are not redacted --------------------------

SNAPSHOT_SAMPLE = ("# agent-sec-snapshot v1\nkeys\t/root/.ssh/authorized_keys\t"
                   "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08 2\n"
                   "sshd\tpasswordauthentication\tno\nsshd\tauthorizedkeysfile\t.ssh/authorized_keys\n"
                   "sshd\ttrustedusercakeys\tnone\nsudoers\t/etc/sudoers.d/agent-ro\t"
                   "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08\n"
                   "units\tmysql.service\tsymlink -> /lib/systemd/system/mysql.service\n"
                   "fw\tufw:22/tcp ALLOW IN Anywhere\trule\n").encode()
AUTH_SAMPLE = ("2026-10-05T23:33:48.847788+03:00 example-stage sshd[607048]: Accepted publickey for root from 198.51.100.2 port 1 ssh2: ED25519 SHA256:abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG\n"
               "2026-10-05T23:33:49.000000+03:00 example-stage sshd[607049]: Failed password for invalid user admin from 198.51.100.5 port 2 ssh2\n").encode()


def test_redact_leaves_snapshot_and_auth_lines_intact():
    assert gate.redact(SNAPSHOT_SAMPLE) == SNAPSHOT_SAMPLE
    assert gate.redact(AUTH_SAMPLE) == AUTH_SAMPLE


def test_a_units_environment_keeps_its_names_and_loses_its_values():
    """Review 04.11.2026: systemctl show/cat print Environment= with values under any name — every value is masked."""
    out = red('Environment=APPCONFIG=rawsecretvalue LANG=C.UTF-8 "OPTS=-a -b"\nEnvironment=\nUser=www\n')
    assert out == 'Environment=APPCONFIG=*** LANG=*** "OPTS=***"\nEnvironment=\nUser=www\n'
    assert count("  Environment=TOKENLESS=abc\n") == 1


def test_a_unit_files_environment_as_written_is_masked_too():
    # review 26.11.2026: systemctl cat prints a unit file as written — spaces around "=", lines continued with "\"
    assert red("Environment = APPCONFIG=rawsecret1\n") == "Environment = APPCONFIG=***\n"
    assert red("Environment\t=\t\"APPCONFIG=raw secret2\"\n") == "Environment\t=\t\"APPCONFIG=***\"\n"
    unit = "Environment=A=rawsecret3 \\\n    B=rawsecret4 \\\n  C=rawsecret5\nUser=www\n"
    assert red(unit) == "Environment=A=*** \\\n    B=*** \\\n  C=***\nUser=www\n"
    assert count(unit) == 3
    # a quoted value continued on the next line keeps its line breaks: a masked file keeps its line numbers
    quoted = 'Environment="OPTS=-a \\\n  -b" X=raw6\nUser=www\n'
    assert red(quoted) == 'Environment="OPTS=***\\\n" X=***\nUser=www\n'
    assert red("environment=a=b\n") == "environment=a=b\n"  # systemd keys are case-sensitive: not a unit's line


def test_a_units_set_credential_value_is_masked():
    assert red("SetCredential=dbcred:rawsecret7\n") == "SetCredential=dbcred:***\n"
    assert red("  SetCredential = api:raw secret8\nUser=www\n") == "  SetCredential = api:***\nUser=www\n"
    assert red("SetCredential=dbcred:\n") == "SetCredential=dbcred:\n"
    assert red("LoadCredential=db:/etc/app/db.cred\n") == "LoadCredential=db:/etc/app/db.cred\n"
