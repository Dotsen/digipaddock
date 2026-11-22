import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from kd import cap_expect, cap_grants
from sysadmin import guard, inventory

HOOK = Path(__file__).resolve().parent.parent / "bin" / "deny_guard.py"

ALLOWED = [
    "ssh-ro example-prod uptime",
    "ssh-ro example-prod 'journalctl -u nginx -n 500' | grep -i error",
    "ssh-exec example-stage 'systemctl restart testapi-partner.example.test.service'",
    'ssh-exec example-prod "systemctl restart x; reboot"',
    "ssh-exec example-prod 'cp config.ini config.ini.bak'",
    "ssh-exec example-prod journalctl --since '1 hour ago'",
    "cat /srv/digipaddock-agents/sysadmin/memory/notes.md",
    "ls /srv/example-docs",
    "ssh-ro example-prod 'journalctl -t sudo -n 20'",
    "ssh-ro node-c 'journalctl -n 50 --no-pager' | grep sudo",
    "ssh-exec example-prod 'echo a#b'",
    "ssh-exec example-prod uptime # just a note",
    "ssh-exec example-prod 'echo {a,b}'",
    "ssh-exec example-prod echo }",  # a lone brace is a literal word for bash (cross-checked in test_shell_calls)
    "ssh-exec example-prod 'echo ${HOME}'",
    "ssh-exec example-prod 'uptime'",
    "ssh-exec example-prod 'echo $(date)'",
    'ssh-exec example-prod "uptime"',
    "ssh-exec example-prod 'mysql -e \"SELECT 1\nFROM app_aggs\"'",
    "ssh-exec --task KD-12 node-c 'cp /etc/nginx/nginx.conf /tmp/digipaddock-nginx.conf && nginx -t'",
    "ssh-exec --task KD-12 node-f,node-h 'apt-get -q update'",
    "ssh-exec --task OLD-12 node-c uptime",  # a card of an earlier prefix of the host (fixture card_aliases)
    "",
]

DENIED = [
    ("ssh-exec example-prod 'iptables -F'", "firewall-flush"),
    ('ssh-exec example-stage "systemctl restart x; reboot"', "stage-power"),
    ("ssh-exec example-prod reboot && ssh-exec example-stage reboot", "stage-power"),
    ("ssh-exec node-a 'sudo -u root iptables -F'", "firewall-flush"),
    ("sudo -u digi-exec /opt/digipaddock/agents/sysadmin/bin/ssh_exec.py example-prod x", "ssh-exec"),
    ("python3 /opt/digipaddock/agents/sysadmin/bin/ssh_exec.py example-prod true", "ssh-exec"),
    ("cat /home/digi-exec/.ssh/agent_exec_ed25519", "digi-exec"),
    ("cat /home/digi/.claude/.credentials.json", "учётка"),
    ("cat /etc/digi-exec/pult.env", "digi-exec"),
    ("sudo systemctl restart nginx", "sudo"),
    ("env LANG=C sudo systemctl restart nginx", "sudo"),
    ("ssh-exec example-stage echo a#b && ssh-exec example-stage reboot", "stage-power"),
    ("cat /var/www/testapi.example.test/config.ini", "паролями"),
    ("grep -r password /var/www/testapi.example.test/.env", "паролями"),
    ("cat /home/digi/.ssh/agent_ro_ed25519", "ключи"),
    ("bash -c \"ssh-exec example-prod 'iptables -F'\"", "формат"),
    ("echo x | xargs ssh-exec example-prod", "формат"),
    ("ssh-exec", "формат"),
    ("ssh-exec --agent x example-prod true", "формат"),
    ("ssh-exec example-prod", "формат"),
    ("ssh-exec example-prod 'unclosed", "кавычки"),
    ('ssh-exec example-prod "$(cat /var/www/testapi.example.test/config.ini)"', "формат"),
    ('ssh-exec example-prod "echo $(</var/www/testapi.example.test/.env)"', "формат"),
    ('ssh-exec example-prod "echo $(cat /etc/mysql/debian.cnf)"', "формат"),
    ('ssh-exec example-prod "echo $(sudo id)"', "формат"),
    ('ssh-exec example-prod "echo `id`"', "формат"),
    ("ssh-exec example-prod $'iptables -F'", "формат"),
    ('ssh-exec example-prod $"iptables -F"', "формат"),
    ("ssh-exec example-prod -- iptables -F", "формат"),
    ("ssh-exec example-stage -- reboot", "формат"),
    ("ssh-exec example-stage '--' reboot", "формат"),
    ("ssh-exec example-prod 2>/dev/null iptables -F", "формат"),
    ("ssh-exec example-prod uptime > /tmp/x", "формат"),
    ("ssh-exec example-prod uptime &> /tmp/x", "формат"),
    ("ssh-exec example-prod cat <<< x", "формат"),
    ("ssh-exec example-prod ipta\\\nbles -F", "формат"),
    ("bash -c 'sudo -n -u agent-ex\"\"ec /opt/digipaddock/agents/sysadmin/bin/ssh_ex\"\"ec.py example-prod uptime'", "исполнитель"),
    ("python3 /opt/digipaddock/agents/sysadmin/bin/ssh_e'x'ec.py example-prod uptime", "исполнитель"),
    ("ssh-exec example-prod uptime # it's fine\nssh-exec example-stage reboot # that's it", "stage-power"),
    ("ssh-exec example-prod uptime # it's fine\nssh-exec example-prod \"echo $(cat /var/www/testapi.example.test/config.ini)\" # that's it", "формат"),
    ("ssh-exec example-prod \"${x:-\"'\"}$(cat /var/www/testapi.example.test/config.ini)\"\'", "формат"),
    ('ssh-exec example-prod "echo ${HOME}"', "формат"),
    ("ssh-exec example-prod {iptables,-F}", "формат"),
    ("ssh-exec --task KD-12 example-prod 'iptables -F'", "firewall-flush"),
    ("ssh-exec --task KD-12 example-stage reboot", "stage-power"),
    ("ssh-exec --task kd-12 node-c uptime", "формат"),
    ("ssh-exec --task KD-12", "формат"),
    ("ssh-exec --task KD-12 node-c", "формат"),
    ('ssh-exec --task KD-12 node-e "$(hostname)"', "формат"),
    ("ssh-exec --task KD-12 --task KD-13 node-c uptime", "формат"),
    ("eval 'sudo ls'", "sudo"),
    ("bash -c 'sudo ls'", "sudo"),
]


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed(command):
    assert guard.decide(command) is None


@pytest.mark.parametrize("command,why", DENIED)
def test_denied(command, why):
    reason = guard.decide(command)
    assert reason is not None and why in reason


NO_CHANNELS = cap_expect.after_bash("sysadmin", grants_root="/nonexistent/kd-cap", root="/nonexistent/kd-expect")


def hook(stdin_text, after_bash=NO_CHANNELS, stdout=None):
    err = io.StringIO()
    code = guard.main(io.StringIO(stdin_text), err, stdout if stdout is not None else io.StringIO(), after_bash=after_bash)
    return code, err.getvalue()


@pytest.fixture
def channels(tmp_path):
    """The sysadmin's self channel with ssh-ro, in tmp_path: (after_bash, expectations dir)."""
    channel = {"key": "self", "run_as": "digi-sysadmin", "programs": [],
               "commands": {"ssh-ro": {"program": "/opt/x/ssh-ro", "args": [], "subcommands": [], "env": {}}}}
    (tmp_path / "g" / "sysadmin").mkdir(parents=True)
    (tmp_path / "g" / "sysadmin" / "self.json").write_bytes(cap_grants.render("sysadmin", 1000, channel))
    return (cap_expect.after_bash("sysadmin", grants_root=str(tmp_path / "g"), root=str(tmp_path / "e"), now=lambda: 10.0),
            tmp_path / "e" / "sysadmin")


def _bash(command):
    return json.dumps({"tool_name": "Bash", "session_id": "s1", "tool_use_id": "toolu_1",
                       "tool_input": {"command": command}})


def test_main_allows_pipeline_and_records_the_channel_call(channels):
    after_bash, store = channels
    out = io.StringIO()
    code, err = hook(_bash("ssh-ro node-c 'journalctl -n 50' | grep -i error"), after_bash, out)
    assert code == 0 and err == ""
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert cap_expect.consume("sysadmin", ["ssh-ro", "node-c", "journalctl -n 50"], root=str(store.parent), now=11.0)


def test_main_refuses_cd_compound(channels):
    code, err = hook(_bash("cd work && cat f"), channels[0])
    assert code == 2 and "отдельным вызовом" in err


def test_guard_denial_leaves_no_expectation(channels):
    after_bash, store = channels
    code, _ = hook(_bash("ssh-ro node-c uptime && ssh-exec example-prod 'iptables -F'"), after_bash)
    assert code == 2 and not store.exists()


def test_main_blocks_with_exit_2_and_reason():
    code, err = hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ssh-exec example-prod 'iptables -F'"}}))
    assert code == 2 and err.startswith("⛔ запрещено правилом firewall-flush")


def test_main_allows_with_exit_0():
    code, err = hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ssh-ro node-c uptime"}}))
    assert code == 0 and err == ""


@pytest.mark.parametrize("stdin_text", ["not json", "[]", json.dumps({"tool_input": {"command": 5}})])
def test_main_fails_closed_on_bad_input(stdin_text):
    code, err = hook(stdin_text)
    assert code == 2 and "внутренняя ошибка" in err


# "uptime", not a channel call: the host's grants would need a tool_use_id the event here does not carry
@pytest.mark.parametrize("command,code", [("ssh-exec example-prod 'iptables -F'", 2), ("uptime", 0)])
def test_entry_point_as_claude_code_runs_it(command, code):
    proc = subprocess.run([sys.executable, "-I", str(HOOK)], input=json.dumps({"tool_input": {"command": command}}),
                          capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == code, proc.stderr


def test_main_blocks_without_tool_input():
    code, err = hook(json.dumps({"tool_name": "Bash"}))
    assert code == 2 and "внутренняя ошибка" in err


def test_entry_point_with_broken_import_blocks(tmp_path):
    lone = tmp_path / "deny_guard.py"  # outside the package: import sysadmin will fail
    lone.write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")
    proc = subprocess.run([sys.executable, "-I", str(lone)], input=json.dumps({"tool_input": {"command": "ls"}}),
                          capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 2 and "не загрузился" in proc.stderr


def test_entry_point_with_garbage_stdin_blocks():
    proc = subprocess.run([sys.executable, "-I", str(HOOK)], input="{", capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 2


DIRECT_EXECUTOR = [
    "ssh_exec.py example-prod true",
    "python3 /opt/digipaddock/agents/sysadmin/bin/ssh_exec.py example-prod true",
    "ls && /opt/digipaddock/agents/sysadmin/bin/ssh_exec.py example-prod true",
]


@pytest.mark.parametrize("command", DIRECT_EXECUTOR)
def test_direct_executor_call_denied(command):
    reason = guard.decide(command)
    assert reason is not None and "исполнитель" in reason


@pytest.mark.parametrize("exc", [RuntimeError("boom"), inventory.InventoryError("пусто")])
def test_main_blocks_when_denylist_raises(monkeypatch, exc):
    def boom(*args, **kwargs):
        raise exc
    monkeypatch.setattr(guard.denylist, "check", boom)
    code, err = hook(json.dumps({"tool_input": {"command": "ssh-exec example-prod 'uptime'"}}))
    assert code == 2 and "внутренняя ошибка" in err


def test_main_blocks_when_join_command_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(guard.inventory, "join_command", boom)
    code, err = hook(json.dumps({"tool_input": {"command": "ssh-exec example-prod uptime"}}))
    assert code == 2 and "внутренняя ошибка" in err


def test_unknown_tool_blocked():
    code, err = hook(json.dumps({"tool_name": "Write", "tool_input": {"file_path": "/x"}}))
    assert code == 2


# --- the final review, A1: bash expands $NAME, globs and ~ outside single quotes before the executor ---

A1_DENIED = [
    'ssh-exec example-prod "$CMD"',
    "ssh-exec example-prod $CMD",
    "ssh-exec example-prod *",
    "cd memory && ssh-exec example-prod *",
    "ssh-exec example-prod systemctl restart example-prod-consumer-*",
    "ssh-exec example-prod cat ~/x",
    'ssh-exec example-prod echo "a$HOME"',
    "ssh-exec example-prod ls /var/log/?.log",
    "ssh-exec example-prod ls /var/log/[a-z]x",
    "ssh-exec example-prod echo x=~/bin",
    "ssh-exec example-prod echo PATH=a:~/bin",
]

A1_ALLOWED = [
    "ssh-exec example-prod 'echo $HOME *'",
    "ssh-exec example-prod 'ls /var/log/*.log ~/x [a]'",
    'ssh-exec example-prod echo "*" "~/x"',  # inside double quotes globs and ~ are not expanded
    "ssh-exec example-prod echo a~b",  # ~ not at the start of a word
    "ssh-exec example-prod echo \\$HOME \\*",  # escaped
    "ls /tmp/*.log; ssh-exec example-prod uptime",  # a glob in the local part is not this check's business
]


@pytest.mark.parametrize("command", A1_DENIED)
def test_a1_expansions_outside_single_quotes_denied(command):
    assert guard.decide(command) == guard.PLAIN


@pytest.mark.parametrize("command", A1_ALLOWED)
def test_a1_literal_forms_allowed(command):
    assert guard.decide(command) is None


# --- the final review, A4: local writes to the agent's settings and environment ---

A4_BLOCKED = [
    "echo '{}' > ~/.claude/settings.json",
    "echo x >> /home/digi/.bashrc",
    "cp /tmp/x /home/digi/.claude/CLAUDE.md",
    "tee -a ~/.profile",
    "sed -i s/a/b/ ~/.config/digipaddock/config.toml",
    "echo x>~/.bash_profile",
    "ln -sf /tmp/x ~/.local/bin/claude",
    "chmod +x $HOME/.bun/bin/bun",
    "truncate -s0 ${HOME}/.claude/settings.json",
    "mv /tmp/x ~digi/.config/x",
    "cd ~/.claude && echo x > settings.json",
    "install -m 0644 /tmp/x /home/digi/.claude/settings.json",
    "chown agent /home/digi/.profile",
    "cp '#' /tmp/x ~/.claude/",
    "echo \"it's\" > ~/.bashrc \"it's\"",
    # the repeated review: >| and copying into a directory
    "echo x >| ~/.bashrc",
    "echo x 2>| ~/.profile",
    "cp /tmp/.bashrc ~/",
    "mv /tmp/.profile /home/digi/",
    "cd ~ && echo x > .profile",
    "cd /home/digi && cp /tmp/x .bashrc",
    # the user's .claude.json (MCP servers, trust in folders) lies outside the directory ~/.claude
    "echo '{}' > ~/.claude.json",
    "cp /tmp/x /home/digi/.claude.json",
    "sed -i s/a/b/ /home/digi/.claude.json",
    "tee ~/.claude.json",
]

A4_NOT_THIS_RULE = [
    "cat ~/.bashrc",
    "cat ~/.claude.json",
    "ls -la ~/.claude",
    "cp ~/.bashrc /tmp/kd-bashrc",
    "echo x > /tmp/kd-a",
    "echo x >> /srv/digipaddock-agents/sysadmin/memory/notes.md",
    "ssh-exec example-prod 'echo x >> ~/.bashrc'",  # a remote command: there ~ is root's home on the server
    "cp /tmp/x ~/",
    "cd ~ && ls > /tmp/kd-l",
    "echo '>|' > /tmp/kd-a",
]


@pytest.mark.parametrize("command", A4_BLOCKED)
def test_a4_local_write_into_agent_config_blocked(command):
    reason = guard.decide(command)
    assert reason is not None and "настройки и окружение агента" in reason, reason


@pytest.mark.parametrize("command", A4_NOT_THIS_RULE)
def test_a4_reads_and_other_writes_pass(command):
    assert guard.decide(command) is None


@pytest.mark.parametrize("command", ["kd-monitor list", "kd-monitor ack P-3 --until 2026-10-06T09:00",
                                     "kd-monitor accept P-4 --label владелец", "kd-monitor baseline all",
                                     "kd-monitor trust 203.0.113.0/24 --label офис", "kd-monitor trust --remove 203.0.113.7",
                                     "kd-monitor fixed P-34 KD-81",  # fixed — waits for the monitor's recheck
                                     'kd-monitor fixed P-34 KD-81 && kd-work finish KD-81 --outcome review --note '
                                     '"fixed, waiting for the monitor recheck"'])
def test_monitor_commands_pass_the_hook(command):
    assert guard.decide(command) is None


# --- the board: only through kd-task; Hindsight's secrets ---

@pytest.mark.parametrize("command", [
    "kd-task claim KD-42",
    'kd-task edit KD-12 --status Review --final-summary "x"',
    'kd-task create "x" --project admin --label server:node-c',
    "kd-task handoff KD-12 --to @boss --comment 'x'",
])
def test_board_commands_pass(command):
    assert guard.decide(command) is None


@pytest.mark.parametrize("command", [
    "cat /srv/digipaddock-agents/board/backlog/tasks/kd-1.md",
    "echo x >> /srv/digipaddock-agents/board/backlog/tasks/kd-1.md",
    "sed -i s/a/b/ '/srv/digipaddock-agents/board/backlog/config.yml'",
    "cat /home/digi/.hindsight/coding-agent.json",
    "grep -r TENANT /etc/hindsight/coding-agent.env",
    "ls ~/.hindsight/",
    "ls /etc/hindsight",
])
def test_board_path_and_hindsight_secrets_denied(command):
    assert guard.decide(command) is not None


@pytest.mark.parametrize("command", [
    "cat /srv/digipaddock-agents/board/backlog/config.yml",
    "kd-task list && cat /srv/digipaddock-agents/board/backlog/config.yml",
    "kd-task list | tee /srv/digipaddock-agents/board/backlog/tasks/kd-1.md",
    "kd-task list > /srv/digipaddock-agents/board/backlog/config.yml",  # a write by redirection — with any command
    "cat /srv//digipaddock-agents/./board/backlog/config.yml",
    "env LANG=C cat /srv/digipaddock-agents/board/backlog/config.yml",
    "bash -c 'cat /srv/digipaddock-agents/board/backlog/config.yml'",
])
def test_board_only_through_kd_task(command):
    assert guard.decide(command) == guard.BOARD_WHY


@pytest.mark.parametrize("command,why", [
    ("cat /home/digi/.hindsight/coding-agent.json", "память Hindsight"),
    ("cat /etc/digipaddock/pult.env", "токен тревог"),
    ("cat /etc/digipaddock/claude.env", "токен Claude"),
    ("cat /etc/digipaddock/llm/openrouter.env", "ключ провайдера"),
])
def test_board_and_hindsight_reasons(command, why):
    reason = guard.decide(command)
    assert reason is not None and why in reason, reason


@pytest.mark.parametrize("command", [
    "kd-task edit KD-1 --notes '[sysadmin] see /srv/digipaddock-agents/board; ok'",  # the path is the note's text
    "kd-task claim KD-7",
])
def test_board_mentions_inside_kd_task_arguments_pass(command):
    assert guard.decide(command) is None


# --- the group ssh-exec: the prohibitions apply to every server of the list ---

GROUP_ALLOWED = [
    "ssh-exec node-f,node-h 'systemctl restart danted'",
    "ssh-exec node-f,example-prod 'reboot'",  # stage-power is for example-stage only
    "ssh-exec node-f,node-h systemctl restart danted",
]

GROUP_DENIED = [
    ("ssh-exec node-f,example-stage 'reboot'", "stage-power"),
    ("ssh-exec example-stage,node-f 'systemctl restart x; reboot'", "stage-power"),
    ("ssh-exec node-f,node-h 'iptables -F'", "firewall-flush"),
    ("ssh-exec node-f,node-h uptime && ssh-exec node-f,example-stage reboot", "stage-power"),
]

GROUP_FORMAT = [
    "ssh-exec node-f,,node-h 'uptime'",
    "ssh-exec node-f, node-h 'uptime'",
    "ssh-exec ,node-f 'uptime'",
    "ssh-exec node-f, 'uptime'",
    "ssh-exec " + ",".join(f"node-x{i}" for i in range(16)) + " 'uptime'",
]


@pytest.mark.parametrize("command", GROUP_ALLOWED)
def test_group_allowed(command):
    assert guard.decide(command) is None


@pytest.mark.parametrize("command,rule", GROUP_DENIED)
def test_group_denylist_checks_every_server(command, rule):
    assert guard.decide(command).startswith(f"запрещено правилом {rule}: ")


@pytest.mark.parametrize("command", GROUP_FORMAT)
def test_group_bad_list_is_format(command):
    assert guard.decide(command) == guard.FORMAT


@pytest.mark.parametrize("command", ["ssh-exec all 'uptime'", "ssh-exec node-f,all 'uptime'",
                                     "ssh-exec all,node-f reboot"])
def test_group_all_is_refused_with_its_own_reason(command):
    assert guard.decide(command) == "ssh-exec: список серверов перечисли явно — all для выполнения не принимается"


@pytest.mark.parametrize("command,why", [
    ("ssh-exec example-prod,node-f 'uptime'", "example-prod — только последним в списке"),
    ("ssh-exec node-f,node-f 'uptime'", "дважды"),
])
def test_group_list_rules_of_the_executor_apply_before_the_card(command, why):
    reason = guard.decide(command)
    assert reason is not None and why in reason


# --- the monitor's state and pages: a local write — a refusal, kd-monitor and reading — let through ---

MONITOR_BLOCKED = [
    "echo '<script>' > /var/lib/digipaddock/monitor-web/index.html",
    "echo x >> /var/lib/digipaddock/monitor/state.json",
    "tee /var/lib/digipaddock/monitor-web/P-35.html",
    "kd-monitor list | tee -a /var/lib/digipaddock/monitor-web/index.html",
    "cp /tmp/kd-x /var/lib/digipaddock/monitor-web/s/node-f.html",
    "cp /tmp/kd-x /var/lib/digipaddock/monitor-web/",
    "mv /tmp/kd-x /var/lib/digipaddock/monitor/state.json",
    "install -m 0640 /tmp/kd-x /var/lib/digipaddock/monitor-web/index.html",
    "ln -sf /etc/passwd /var/lib/digipaddock/monitor-web/P-1.html",
    "rm /var/lib/digipaddock/monitor/state.json",
    "rm -rf /var/lib/digipaddock",
    "sed -i s/a/b/ /var/lib/digipaddock/monitor/state.json",
    "truncate -s0 /var/lib/digipaddock/monitor/state.json",
    "chmod 0644 /var/lib/digipaddock/monitor/state.json",
    "cd /var/lib/digipaddock/monitor && echo '{}' > state.json",
    "echo x > /var/lib//digipaddock/./monitor-web/index.html",
    # the tree's parent — as in cage-write
    "rm -r /var/lib", "rm -rf /var/lib/", "rm -r /var", "chmod -R 777 /var/lib", "chown -R nobody /var/lib",
    "mv /var/lib /tmp/kd-x", "mv /var/lib/digipaddock /tmp/kd-x", "rmdir /var/lib/digipaddock/monitor-web/s",
    # bash expands globs in a path — the parser matches them against the tree
    "echo x > /var/lib/digipaddoc*/monitor-web/index.html", "echo x>/var/lib/digipaddoc[k]/monitor/state.json",
    "cp /tmp/kd-x /var/lib/digipaddoc*/monitor-web/", "rm -rf /var/lib/digipaddoc*", "rm -r /var/l?b/digipaddock",
    "tee /var/lib/d*/monitor/state.json", "rm -r /var/*", "dd if=/tmp/kd-x of=/var/lib/digipaddoc*/monitor/state.json",
    "rm -rf /var/lib/x/../digipaddoc*",
    # touch and mkdir (the security review): a directory in a page's place — os.replace fails with EISDIR and all pages
    # stop; a fresh date on index.html silences the morning line about frozen pages
    "mkdir -p /var/lib/digipaddock/monitor-web/index.html", "mkdir /var/lib/digipaddock/monitor-web/P-35.html",
    "mkdir -m 0755 /var/lib/digipaddoc*/monitor-web/s/node-f.html",
    "cd /var/lib/digipaddock/monitor-web && mkdir -p s/node-f.html",
    "touch /var/lib/digipaddock/monitor-web/index.html", "touch -d '2026-10-06 09:00' /var/lib/digipaddock/monitor-web/index.html",
    "touch -r /tmp/kd-x /var/lib/d*/monitor-web/index.html", "kd-monitor list && touch /var/lib/digipaddock/monitor/state.json",
]


@pytest.mark.parametrize("command", MONITOR_BLOCKED)
def test_local_write_into_monitor_state_blocked(command):
    assert guard.decide(command) == guard.MONITOR_WHY


@pytest.mark.parametrize("command", [
    "kd-monitor list", "kd-monitor ack P-3 --until 2026-10-06T09:00", "kd-monitor accept P-4 --label x",
    "cat /var/lib/digipaddock/monitor/state.json", "ls -l /var/lib/digipaddock/monitor-web",
    "grep P-35 /var/lib/digipaddock/monitor/state.json", "ls -l /var/lib",
    "cp /var/lib/digipaddock/monitor/state.json /tmp/kd-state.json",
    "ls /var/lib/digipaddoc*/monitor-web", "cat /var/lib/digipaddoc*/monitor/state.json",
    "cp /var/lib/digipaddoc*/monitor/state.json /tmp/kd-s", "rm /tmp/kd-*", "rm -r /var/lib/other-*",
    "touch /tmp/kd-x", "mkdir -p /tmp/kd-d/s", "ls -ld /var/lib/digipaddock/monitor-web/s",
    "kd-monitor baseline node-f", "kd-monitor trust 198.51.100.2 --label home",
])
def test_monitor_commands_and_reads_pass(command):
    assert guard.decide(command) is None


@pytest.mark.parametrize("command", [
    "mv /tmp/kd-x /var/lib/digipaddock/monitor/baseline.json",
    "cp work/tmp/b.json /var/lib/digipaddock/monitor/baseline.json",
    "jq '.trusted[\"198.51.100.2\"]=\"x\"' /var/lib/digipaddock/monitor/baseline.json > work/tmp/b.json",
    "python3 -c \"import json; open('/var/lib/digipaddock/monitor/baseline.json', 'w').write('{}')\"",
    "echo '{}' > /var/lib/digipaddoc*/monitor/baseline.json",
    "tee /var/lib/digipaddock//monitor//baseline.json",
])
def test_the_baseline_is_never_written_around_the_owners_decision(command):
    """The agent adds nothing to the baseline past monitor_trust — neither by a write nor by a script."""
    reason = guard.decide(command)
    assert reason is not None and "monitor_trust" in reason, reason


@pytest.mark.parametrize("command", [
    "KD_MONITOR_STATE=/tmp/kd-x kd-monitor tick",  # tick would publish pages from a planted state
    "env KD_MONITOR_CONFIG=/tmp/kd-c.json kd-monitor list",
    "export KD_MONITOR_STATE=/tmp/kd-x; kd-monitor list",
    "bash -c 'KD_MONITOR_STATE=/tmp/kd-x kd-monitor tick'",
])
def test_monitor_env_override_blocked(command):
    reason = guard.decide(command)
    assert reason is not None and "KD_MONITOR_* не переопределяются" in reason, reason


def test_forge_secrets_blocked_for_sysadmin():
    # ssh-exec runs as root: the denylist is frozen and closes only writes — reading forge tokens is closed by the hook.
    # All three commands passed without the new rule (a key outside .ssh/ — neither LOCAL nor the denylist sees it)
    for cmd in ("ssh-exec example-stage 'cat /etc/digipaddock/forge/digipaddock-merge.env'",
                "ssh-ro example-stage 'ls -la /home/digi-forge'",
                "ssh-exec example-stage 'cat /root/dev_ed25519'"):
        reason = guard.decide(cmd)
        assert reason is not None and "токены Forgejo" in reason and "digi-forge" in reason, (cmd, reason)
    assert guard.decide("ssh-ro example-stage 'tail -n 5 /var/log/digipaddock-forge/forge.jsonl'") is None


@pytest.mark.parametrize("cmd", [
    "cat /home/digi-sysadmin/.ssh/agent_ro_ed25519",
    "cat /home/digi-sysadmin/.claude/.credentials.json",
    "cat /home/digi/.ssh/agent_ro_ed25519",  # the old home stays secret
    "echo x >> /home/digi-sysadmin/.bashrc",
    "cp /tmp/x /home/digi-sysadmin/.claude/settings.json",
    "echo x >> ~/.bashrc",  # ~ is the agent's own home now
    "echo x >> /home/digi/.bashrc",
])
def test_both_homes_are_guarded(cmd):
    assert guard.decide(cmd)


# Claude Code 2.1.289 asks the owner for `ssh-ro node-c cat /etc/passwd` despite Bash(ssh-ro *) in allow —
# an unquoted argument that is an existing local path outside the working dirs, or a bare «=», goes to a card, and
# a hook «allow» does not override it. The same command as one quoted argument passes (probe on stage, 09.10.2026).
@pytest.mark.parametrize("command", [
    "ssh-ro node-c cat /etc/passwd",
    "ssh-ro node-c,node-e cat /etc/hostname",
    "ssh-ro node-c ls -la /var/log/nginx",
    "ssh-ro node-c find /opt /etc -maxdepth 3 -name docker-compose.yml",
    "ssh-ro node-c ss -tln sport = :3030",
    "ssh-ro example-stage ss -tn '(' dport = :443 ')'",
    "ssh-ro node-c cat ~/x",
    "ssh-ro node-c cat /etc/passwd | grep -i amir",
    "ssh-ro node-c uptime; ssh-ro node-c ls -la /home",
    "ssh-ro node-c cat '/etc/hostname'",
])
def test_ssh_ro_unquoted_path_or_equals_is_refused_before_the_card(command):
    reason = guard.decide(command)
    assert reason is not None and "ssh-ro" in reason and "кавычк" in reason


@pytest.mark.parametrize("command", [
    "ssh-ro node-c 'cat /etc/passwd'",
    "ssh-ro node-c,node-e 'cat /etc/hostname'",
    "ssh-ro node-c 'cat /etc/hostname' | head -n 1",
    "ssh-ro node-c 'grep -rn 3030 /etc/nginx/sites-enabled' 2>&1 | tail -n 3",
    "ssh-ro node-c 'ss -tln sport = :3030'",
    "ssh-ro example-stage 'ss -tn ( dport = :443 )'",
    "ssh-ro node-c docker ps",
    "ssh-ro node-c uptime",
    """ssh-ro example-prod mysql -e "SELECT COUNT(*) FROM t WHERE status='failed'\"""",
    "ssh-ro node-c 'tail -n 5 /var/log/syslog' > /srv/digipaddock-agents/sysadmin/work/tmp/syslog.txt",
    "ssh-ro node-c uptime 2>/dev/null",
    "ssh-ro node-c '/usr/bin/uptime -p'",
    "ssh-ro node-c '~/bin/x --help'",
])
def test_ssh_ro_quoted_or_plain_forms_pass(command):
    assert guard.decide(command) is None


# the gate's exact root form for fingerprints names root's key directory — only as the ssh-ro remote
# command
@pytest.mark.parametrize("command", [
    "ssh-ro node-d 'ssh-keygen -lf /root/.ssh/authorized_keys'",
    "ssh-ro all 'ssh-keygen -lf /root/.ssh/authorized_keys'",
    "ssh-ro node-d,node-c 'ssh-keygen -lf /root/.ssh/authorized_keys' | grep -c SHA256",
    "uptime; ssh-ro node-c 'ssh-keygen -lf /root/.ssh/authorized_keys'",
    "ssh-ro node-d 'grep -n -e \"Accepted|Failed\" /var/log/auth.log'",
])
def test_ssh_ro_fingerprint_form_passes(command):
    assert guard.decide(command) is None


@pytest.mark.parametrize("command", [
    "ssh-ro node-d 'cat /root/.ssh/authorized_keys'",
    "ssh-ro node-d 'ssh-keygen -lf /root/.ssh/authorized_keys' && cat /root/.ssh/authorized_keys",
    "echo 'ssh-keygen -lf /root/.ssh/authorized_keys'",
    "ssh-keygen -lf /root/.ssh/authorized_keys",
    "x ssh-ro node-d 'ssh-keygen -lf /root/.ssh/authorized_keys'",
])
def test_ssh_dir_stays_closed_elsewhere(command):
    assert guard.decide(command) == "ключи SSH недоступны"


# the exec gate's mode and its journal records — read-only, only as the whole ssh-ro remote command
@pytest.mark.parametrize("command", [
    "ssh-ro node-d 'cat /etc/agent-exec-gate/gate.json'",
    "ssh-ro all 'cat /etc/agent-exec-gate/gate.json'",
    "ssh-ro node-d,node-c 'journalctl -t agent-exec-gate --no-pager'",
    "ssh-ro node-d 'journalctl -t agent-exec-gate --no-pager -n 200'",
    "ssh-ro node-d 'journalctl -t agent-exec-gate --no-pager -n 50 --since 2026-11-16'",
    "ssh-ro node-d 'grep -F agent-exec-gate /var/log/auth.log'",
    "ssh-ro node-a 'grep -F agent-exec-gate /var/log/secure'",
    "ssh-ro all 'journalctl -t agent-exec-gate --no-pager -n 500' | grep -c deny",
    "uptime; ssh-ro node-c 'cat /etc/agent-exec-gate/gate.json'",
])
def test_ssh_ro_exec_gate_reads_pass(command):
    assert guard.decide(command) is None


@pytest.mark.parametrize("command", [
    "ssh-ro node-d 'cat /etc/agent-exec-gate/signers.json'",
    "ssh-ro node-d 'cat /etc/agent-exec-gate/gate.json /home/digi-exec/x'",
    "ssh-ro node-d 'journalctl -t agent-exec-gate'",
    "ssh-ro node-d 'journalctl -t agent-exec-gate --no-pager -n 50 -f'",
    "ssh-ro node-d 'grep -F agent-exec-gate /var/log/syslog'",
    "ssh-ro node-d 'cat /etc/agent-exec-gate/gate.json' # agent-exec-gate",
    "ssh-ro node-d 'cat /etc/agent-exec-gate/gate.json' && cat /home/digi-exec/.ssh/agent_exec_ed25519",
    "ssh-ro node-d 'cat /etc/agent-exec-gate/gate.json' | grep agent-exec-gate",
    "echo 'cat /etc/agent-exec-gate/gate.json'",
    "cat /etc/agent-exec-gate/gate.json",
    "x ssh-ro node-d 'cat /etc/agent-exec-gate/gate.json'",
    "ssh-ro node-d cat /etc/agent-exec-gate/gate.json",
    "ssh-exec --task KD-1 node-d 'cat /etc/agent-exec-gate/gate.json'",
    "ssh-ro node-d 'cat /etc/agent-exec-gate/gate.json' 'cat /etc/agent-exec-gate/gate.json'",
])
def test_exec_gate_name_stays_closed_elsewhere(command):
    assert guard.decide(command) == f"пользователь {guard.EXEC_USER} и exec-ключ агенту недоступны"
