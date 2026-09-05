"""Settings, and what happens to the ones that cannot be read.

A setting nobody can see failing is worse than one that is rejected: the
reader believes they configured a stall detector and never hears from it.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest

from pytest_failure_instrumentation import config as settings_module
from pytest_failure_instrumentation.config import (
    MIN_HEARTBEAT_INTERVAL,
    FailureInstrumentationWarning,
    resolve,
)


class FakeConfig:
    """Just enough of pytest.Config to resolve settings against."""

    def __init__(self, options: dict | None = None, **values: object) -> None:
        self.values = values
        self.options = options or {}

    def getini(self, name: str) -> object:
        return self.values.get(name, DEFAULTS[name])

    def getoption(self, name: str) -> object:
        return self.options.get(name)


DEFAULTS = {
    "failure_directory": ".pytest-failures",
    "failure_packages": [],
    "failure_product_version": "",
    "failure_watchdog": "true",
    "failure_heartbeat_interval": "5.0",
    "failure_tracemalloc_depth": "0",
    "failure_object_census": "false",
    "failure_high_water_mb": "0",
    "failure_memory_limit_mb": "0",
    "failure_slow_test_seconds": "20",
    "failure_stall_seconds": "300",
    "failure_stack_probe": "true",
    "failure_crash_stack": "false",
    "failure_capture_output": "false",
    "failure_kill_trace": "true",
    "failure_elevate": "false",
    "failure_on_run_death": "",
    "failure_tracer": "parent",
    "failure_sample_seconds": "0",
    "failure_stack_server": "false",
    "failure_stack_server_port": "0",
    "failure_stack_server_host": "127.0.0.1",
    "failure_stack_server_locals": "true",
    "failure_profile": "false",
    "failure_profile_interval": "0.02",
    "failure_profile_cpu_share": "5",
    "failure_profile_cpu_floor_seconds": "0.5",
    "failure_profile_retained_mb": "100",
    "failure_profile_peak_mb": "0",
    "failure_profile_allocations": "false",
    "failure_profile_allocation_depth": "12",
    "failure_profile_burst_cores": "0.7",
    "failure_profile_burst_seconds": "2",
}


def test_a_setting_that_is_not_a_number_says_so_rather_than_vanishing():
    with pytest.warns(FailureInstrumentationWarning, match="failure_stall_seconds"):
        resolved = resolve(FakeConfig(failure_stall_seconds="5m"))
    assert resolved.stall_seconds == 300.0


def test_a_readable_setting_warns_about_nothing():
    # Recorded rather than filtered to an error. This plugin's advice is now
    # deliberately immune to an error filter - a project running
    # ``filterwarnings = error`` used to get INTERNALERROR out of
    # pytest_configure instead of the advice - so an error filter no longer
    # detects an unwanted warning here. Counting them does.
    with warnings.catch_warnings(record=True) as raised:
        warnings.simplefilter("always")
        assert resolve(FakeConfig(failure_stall_seconds="45")).stall_seconds == 45.0
    assert [str(entry.message) for entry in raised] == []


def test_the_heartbeat_interval_is_clamped_where_both_sides_can_see_it():
    """The worker used to clamp this and the controller did not.

    Two answers to "how often does a worker beat" put the controller's
    staleness window inside the worker's own cadence, and a blocked worker
    confirmed as frozen - reported as native code holding the GIL.
    """
    resolved = resolve(FakeConfig(failure_heartbeat_interval="0.05"))
    assert resolved.heartbeat_interval == MIN_HEARTBEAT_INTERVAL

    from pytest_failure_instrumentation.capture.heartbeat import Heartbeat

    beating = Heartbeat(lambda *a, **k: None, interval=resolved.heartbeat_interval)
    assert beating.interval == resolved.heartbeat_interval


def test_a_worker_takes_the_run_id_the_controller_pushed_down():
    class Worker(FakeConfig):
        workerinput = {"workerid": "gw3", "workercount": 4, "failure_run_id": "run-abc"}

    assert resolve(Worker()).run_id == "run-abc"
    # A plain run has no controller to be told by, and says so with None
    # rather than inventing one that would match nothing.
    assert resolve(FakeConfig()).run_id is None


class RecordingParser:
    """Both halves of what add_options registers, with the help kept.

    The command-line options go to a group, and a fake that cannot hand one
    out would make a test pass by never reaching them. The help text is
    recorded because it is not decoration: it is the whole of what ``pytest
    --help`` shows, so a sentence that stopped being true there is a sentence
    nobody has any other way to catch.
    """

    def __init__(self) -> None:
        self.ini: dict[str, str] = {}
        self.command_line: dict[str, str] = {}

    def addini(self, name, help, type=None, default=None):  # noqa: A002
        self.ini[name] = help

    def getgroup(self, name):
        return self

    def addoption(self, name, **kwargs):
        self.command_line[name] = kwargs.get("help", "")


def _registered() -> RecordingParser:
    """Everything ``add_options`` registers, as pytest would have it."""
    parser = RecordingParser()
    settings_module.add_options(parser)
    return parser


def _settings_named_in_the_readme_table() -> set[str]:
    """The names in the README's settings table, read out of the table.

    Found from this file rather than from the package, the way the incident
    kinds are checked against the hookspec and the triage skill: the tests
    only ever ship with the source, while the package is also run out of a
    built wheel in site-packages with no README above it.
    """
    readme = Path(__file__).resolve().parent.parent / "README.md"
    if not readme.exists():
        pytest.skip("the README is not part of this checkout")

    # The table, and nothing after it: the prose below it names settings too,
    # and a setting explained there but missing from the table is exactly the
    # drift this is looking for.
    table = readme.read_text(encoding="utf-8").partition("\n## Settings\n")[2]
    return set(re.findall(r"^\|\s*`(failure_\w+)`", table.partition("\n## ")[0], re.MULTILINE))


def test_every_setting_in_the_readme_table_is_registered():
    """The table is how anybody finds these, so a setting that drifts out of it
    is a setting nobody can turn on.

    Which means reading the table. This asserted against ``DEFAULTS`` alone -
    a dict a few lines up in this same file - so what it actually guarded was
    that two lists in the source agreed with each other, and a setting added
    to ``config.py`` and to ``DEFAULTS`` and documented nowhere passed it. The
    README is now read the way ``test_models`` reads the hookspec table and the
    triage skill, and for the same reason: the copy that goes stale unnoticed
    is always the one no test opens.

    ``DEFAULTS`` is still compared, because it is what the fake config in this
    module answers ``getini`` from - a setting missing from it makes every
    other test here raise KeyError rather than fail with a reason.
    """
    parser = _registered()
    assert sorted(parser.command_line) == [
        "--callstack-host",
        "--callstack-port",
        "--callstack-token",
        "--failure-instrumentation",
        "--failure-profile",
        "--failure-profile-allocations",
    ]
    assert sorted(parser.ini) == sorted(DEFAULTS)
    assert sorted(parser.ini) == sorted(_settings_named_in_the_readme_table())


def test_the_stack_server_help_describes_the_server_that_ships():
    """``pytest --help`` is where a CI operator reads what this switches on,
    and what it read there was one design old.

    It promised "one server per host, shared by every pytest session on it"
    and "loopback only". Neither survived: the default port is *drawn*, so a
    session serves itself and shares with nobody unless a port is named, and
    ``failure_stack_server_host`` binds whatever it is given - 0.0.0.0 is the
    documented container configuration, refused only when no token is
    supplied. This is the one place in the package that promised a loopback
    bind, which is exactly the promise an operator would have planned around.
    """
    help_text = _registered().ini["failure_stack_server"]
    assert "loopback only" not in help_text.lower()
    assert "one server per host" not in help_text.lower()
    # And says the two true things in their place.
    assert "drawn for it" in help_text
    assert "failure_stack_server_host" in help_text


def test_the_token_option_says_that_a_command_line_is_readable():
    """The help is what ``pytest --help`` shows, so it is where somebody about
    to type a secret into argv can still be told not to."""
    help_text = _registered().command_line["--callstack-token"]
    assert settings_module.TOKEN_ENV in help_text
    assert "ps -eww" in help_text and "/proc/<pid>/cmdline" in help_text


def test_the_shipped_defaults_leave_a_stall_something_to_read():
    """These two settings look independent and are not.

    The stack a stalled worker is reported with is whatever the watchdog last
    wrote, so the cadence has to have fired before the stall is assessed. At
    the old 120s cadence a worker wedged for a minute left nothing at all -
    and on Windows, where nothing can ask a live process for a stack, that was
    every stall shorter than two minutes.
    """
    defaults = resolve(FakeConfig())
    assert 0 < defaults.slow_test_seconds < defaults.stall_seconds


def test_an_inverted_pair_says_what_it_will_cost():
    with pytest.warns(FailureInstrumentationWarning, match="before its watchdog"):
        resolve(FakeConfig(failure_slow_test_seconds="60", failure_stall_seconds="10"))


def test_switching_either_one_off_is_not_an_inversion():
    with warnings.catch_warnings(record=True) as raised:
        warnings.simplefilter("always")
        resolve(FakeConfig(failure_stall_seconds="0"))       # no stall detection
        resolve(FakeConfig(failure_slow_test_seconds="0"))   # no watchdog
        resolve(FakeConfig(failure_watchdog="false"))
    assert [str(entry.message) for entry in raised] == []


def test_the_two_places_the_version_is_written_agree():
    """The release workflow reads pyproject and refuses a tag that disagrees
    with it. It never reads ``__version__``.

    So that one can drift unnoticed: a release publishing 0.2.0 while the
    installed package reports 0.1.0 to anyone who asks it. Nothing else
    catches that, because the check that exists is between the tag and
    pyproject, not between pyproject and here.

    Read with a regex rather than tomllib, which is 3.11+, and against the
    source rather than the installed metadata, which is written at install
    time and goes stale the moment pyproject is edited.

    pyproject is found from this file rather than from the package, because
    the package is not always in the tree that declares it: the release
    workflow runs this same suite against the built wheel, where it sits in
    site-packages with no pyproject.toml anywhere above it. The tests only
    ever ship with the source, so their own location is the one that finds
    it - and against an installed wheel the comparison gets stronger, since
    the version being read out is the one that was packaged.
    """
    import pytest_failure_instrumentation

    root = Path(__file__).resolve().parent.parent
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    assert declared, "no version in pyproject.toml"
    assert pytest_failure_instrumentation.__version__ == declared.group(1)


# -- where this run happens, as against what this project wants -----------


def test_no_port_asked_for_means_one_is_drawn():
    """The default is the lottery, because a session nobody has told which port
    to use has no reason to fight the other sessions for one."""
    assert resolve(FakeConfig()).stack_server_port == 0
    assert resolve(FakeConfig()).stack_server_host == "127.0.0.1"


def test_the_command_line_outranks_ini_for_the_address(monkeypatch):
    """These answer "where does this run happen", which the person starting it
    knows and the repository does not.

    The token is there so the bind is not refused, and it comes from the
    environment so that argv's own advice about a token typed there stays out
    of a test about the address.
    """
    monkeypatch.setenv(settings_module.TOKEN_ENV, "s3cret")
    resolved = resolve(
        FakeConfig(
            {
                "callstack_port": 9111,
                "callstack_host": "0.0.0.0",
            },
            failure_stack_server_port="8080",
            failure_stack_server_host="127.0.0.1",
            failure_stack_server="true",
        )
    )
    assert resolved.stack_server_port == 9111
    assert resolved.stack_server_host == "0.0.0.0"


def test_naming_either_one_on_the_command_line_switches_the_server_on(monkeypatch):
    """An option that is accepted, parsed and then ignored because a separate
    ini flag was left at its default is the worst available behaviour."""
    monkeypatch.setenv(settings_module.TOKEN_ENV, "s3cret")
    assert resolve(FakeConfig({"callstack_port": 9111})).stack_server
    assert resolve(FakeConfig({"callstack_host": "0.0.0.0"})).stack_server
    assert not resolve(FakeConfig()).stack_server


def test_binding_off_loopback_with_a_token_is_not_warned_about(monkeypatch):
    """A bind off loopback is a decision, and with a token it is a complete
    one: the exposure is deliberate, and it is what a container whose UI lives
    outside it needs. It used to be advised about anyway, on every run, and
    what the advice said was the address the person had just typed - in the
    one place it is typed for, at every start of every job. Without a token
    the same bind is refused rather than warned about (see below), so there is
    no case left in which this configuration has anything to say.

    The token comes from the environment rather than the command line so that
    argv's own advice, which has nothing to do with the bind, stays out of it.
    """
    monkeypatch.setenv(settings_module.TOKEN_ENV, "s3cret")
    with warnings.catch_warnings(record=True) as raised:
        warnings.simplefilter("always")
        resolve(FakeConfig({"callstack_host": "0.0.0.0"}))
    assert [str(entry.message) for entry in raised] == []


def test_loopback_by_any_of_its_names_is_not_an_exposure():
    with warnings.catch_warnings(record=True) as raised:
        warnings.simplefilter("always")
        for name in ("127.0.0.1", "::1", "localhost"):
            resolve(FakeConfig({"callstack_host": name}))
    assert [str(entry.message) for entry in raised] == []


def test_advice_about_a_setting_cannot_end_the_run_that_asked_for_it(pytester):
    """``filterwarnings = error`` is a recommended pytest setting and a common
    one. Every warning this plugin raises about a setting comes out of
    ``pytest_configure``, and an exception thrown from a configure hook is an
    INTERNALERROR: exit status 3, not one test collected.

    Measured on the branch that added it: ``--callstack-host 0.0.0.0`` - the
    documented way to reach the live view from outside a container, which at
    the time warned about the bind - ended the session that way on any project
    with that filter. So did an unreadable ``failure_stall_seconds``. The
    class docstring has always said these are "never an error"; now they
    cannot be. The bind no longer warns; the token typed on the same command
    line still does, and that is the advice this run now has to survive.
    """
    pytester.makeini(
        """
        [pytest]
        filterwarnings =
            error
        """
    )
    pytester.makepyfile("def test_ok():\n    assert True\n")

    result = pytester.runpytest_subprocess(
        "--callstack-host", "0.0.0.0", "--callstack-token", "s3cret"
    )

    result.assert_outcomes(passed=1)
    assert result.ret == 0, result.stdout.str()
    assert "INTERNALERROR" not in result.stdout.str()
    # Downgraded, not dropped: the advice is still given, in the same place it
    # lands on a project with no filter at all.
    assert "--callstack-token puts the token" in result.stderr.str()


def test_the_token_comes_from_the_command_line_or_the_environment(monkeypatch):
    """Two places, and deliberately not a third. ini files live in the
    repository, and a credential in the repository is exactly what minting one
    per server and publishing it beside the port amounted to."""
    monkeypatch.delenv(settings_module.TOKEN_ENV, raising=False)
    assert resolve(FakeConfig({"callstack_port": 8080})).stack_server_token == ""

    monkeypatch.setenv(settings_module.TOKEN_ENV, "from-the-environment")
    assert (
        resolve(FakeConfig({"callstack_port": 8080})).stack_server_token
        == "from-the-environment"
    )

    # The command line outranks it, which is what lets one run differ from the
    # shell it was started in - and is advised against on its own account,
    # because argv is readable by everyone on the machine and an environment
    # is not. See test_a_token_on_the_command_line_says_who_else_can_read_it.
    with pytest.warns(FailureInstrumentationWarning, match="command line"):
        from_the_command_line = resolve(
            FakeConfig({"callstack_port": 8080, "callstack_token": "from-the-cli"})
        )
    assert from_the_command_line.stack_server_token == "from-the-cli"


def test_a_token_alone_does_not_start_a_server(monkeypatch):
    """Naming a port or a host says "serve"; naming a secret says "and
    authenticate it". An exported PYTEST_CALLSTACK_TOKEN sitting in somebody's
    shell profile must not open a listening socket on every pytest run in that
    shell."""
    monkeypatch.setenv(settings_module.TOKEN_ENV, "s3cret")
    resolved = resolve(FakeConfig())
    assert resolved.stack_server is False
    assert resolved.stack_server_token == "s3cret"


def test_off_loopback_without_a_token_is_refused_rather_than_warned_about(monkeypatch):
    """Nobody configures "serve every local process's stack to the network",
    so it is not a decision to warn about - it is one to decline. With a token
    it becomes a decision, and is left alone."""
    monkeypatch.delenv(settings_module.TOKEN_ENV, raising=False)
    open_to_the_world = resolve(FakeConfig({"callstack_host": "0.0.0.0"}))
    assert open_to_the_world.refuses_to_bind_unauthenticated

    with warnings.catch_warnings(record=True) as raised:
        warnings.simplefilter("always")
        resolve(FakeConfig({"callstack_host": "0.0.0.0"}))
    # Refused, so there is nothing here to advise about.
    assert [str(entry.message) for entry in raised] == []

    monkeypatch.setenv(settings_module.TOKEN_ENV, "s3cret")
    guarded = resolve(FakeConfig({"callstack_host": "0.0.0.0"}))
    assert not guarded.refuses_to_bind_unauthenticated


def test_loopback_needs_no_token_and_is_not_refused(monkeypatch):
    """The default, and the case the headache was being paid for."""
    monkeypatch.delenv(settings_module.TOKEN_ENV, raising=False)
    for name in ("127.0.0.1", "::1", "localhost"):
        resolved = resolve(FakeConfig({"callstack_host": name}))
        assert not resolved.refuses_to_bind_unauthenticated
        assert resolved.stack_server_token == ""


def test_a_token_on_the_command_line_says_who_else_can_read_it(monkeypatch):
    """``--callstack-token SECRET`` hands the token to every other account on
    the machine, which is the machine the token exists to protect.

    A process's argv is world-readable - ``/proc/<pid>/cmdline`` on Linux,
    ``ps -eww`` anywhere - and a controller lives as long as the run, so a
    local user has the length of the suite to read it out. The environment
    variable reaches the same field and does not: ``/proc/<pid>/environ`` is
    0400. The flag is not refused, because runs already use it and it is
    unobjectionable where there is one user; it just no longer happens
    silently.
    """
    monkeypatch.delenv(settings_module.TOKEN_ENV, raising=False)
    with pytest.warns(FailureInstrumentationWarning) as raised:
        resolved = resolve(FakeConfig({"callstack_port": 8080, "callstack_token": "s3cret"}))

    said = "\n".join(str(entry.message) for entry in raised)
    assert settings_module.TOKEN_ENV in said, said
    # Advice about a credential being readable that quotes the credential has
    # published it a second time, into the warnings summary and the CI log.
    assert "s3cret" not in said
    # Nothing is refused: the token asked for is the token in force.
    assert resolved.stack_server_token == "s3cret"


def test_a_token_from_the_environment_is_advised_about_nothing(monkeypatch):
    """The form that does not leak is the one nobody should be nagged about -
    advice that fires on the right answer as well as the wrong one is noise,
    and this fires once per run."""
    monkeypatch.setenv(settings_module.TOKEN_ENV, "s3cret")
    with warnings.catch_warnings(record=True) as raised:
        warnings.simplefilter("always")
        resolve(FakeConfig({"callstack_port": 8080}))
    assert [str(entry.message) for entry in raised] == []


def test_a_worker_does_not_repeat_what_the_controller_already_said(monkeypatch):
    """A worker parses the same argv as its controller, so the advice would
    otherwise arrive once per worker - eight copies of it under ``-n8``, which
    is how a real caveat turns into something people filter out."""
    monkeypatch.delenv(settings_module.TOKEN_ENV, raising=False)

    class Worker(FakeConfig):
        workerinput = {
            "workerid": "gw1",
            "workercount": 2,
            "failure_settings": settings_module.Settings().as_payload(),
        }

    with warnings.catch_warnings(record=True) as raised:
        warnings.simplefilter("always")
        resolve(Worker({"callstack_port": 8080, "callstack_token": "s3cret"}))
    assert [str(entry.message) for entry in raised] == []


def test_the_sampler_cadence_has_a_floor_like_its_sibling():
    """Each pass walks the run directory, reads every state file, tails every
    event log and may spawn a py-spy per stuck worker. Below a second that is
    a busy loop wearing a setting's clothes - and heartbeat_interval, which
    does far less per tick, has been clamped since it was written."""
    assert settings_module.Settings(sample_seconds=0.01).sample_seconds == (
        settings_module.MIN_SAMPLE_SECONDS
    )
    # Off stays off: 0 is "do not sample", not "sample as fast as allowed".
    assert settings_module.Settings(sample_seconds=0).sample_seconds == 0.0
    assert settings_module.Settings(sample_seconds=-5).sample_seconds == 0.0
    # And a sane value is left alone.
    assert settings_module.Settings(sample_seconds=30).sample_seconds == 30.0


# -- who may read a worker, and whether anybody asked -----------------------


def test_an_unusable_tracer_policy_says_so_rather_than_granting_one():
    """The one fallback in this file that hands out a permission.

    "none" is what somebody who wants no ptrace declaration reaches for, and
    it is not one of the three words this setting takes. It used to resolve to
    "parent" with nothing said anywhere, so a reader who believed they had
    withheld the permission had granted it - the one direction a permission
    setting must not fail in quietly. The fallback stands, because a typo in
    an ini file is still not worth ending a run over; it is now audible, like
    every other unusable value here.
    """
    with pytest.warns(FailureInstrumentationWarning, match="failure_tracer"):
        assert resolve(FakeConfig(failure_tracer="none")).tracer == "parent"

    # The hand-built path cannot skip it either: a framework computes this
    # value in Python, which is where a typo is least likely to be read.
    with pytest.warns(FailureInstrumentationWarning, match="parent, any, off"):
        assert settings_module.Settings(tracer="nope").tracer == "parent"


def test_a_policy_that_differs_only_in_case_is_the_policy_it_looks_like():
    """Not a typo and not reported as one. ``resolve`` has always folded case
    for the ini path, so a framework passing "OFF" to install() must mean
    there what it would have meant in an ini file."""
    with warnings.catch_warnings(record=True) as raised:
        warnings.simplefilter("always")
        assert settings_module.Settings(tracer=" OFF ").tracer == "off"
    assert [str(entry.message) for entry in raised] == []


def test_a_run_that_reads_no_worker_stacks_declares_no_tracer():
    """Nobody reading means nothing to permit.

    ptrace_scope=1 is the Ubuntu and Debian default, so before this every
    worker of every Linux run declared a tracer at startup whether or not the
    run had a live view - an upgrade and ``pytest -n8`` widened who may read a
    test process, for a feature nobody had switched on. And it is a real
    widening: Yama admits the nominated pid *and its descendants*, which is
    the whole process tree of the run.
    """
    assert settings_module.Settings().tracer_in_force == "off"
    assert settings_module.Settings(tracer="any").tracer_in_force == "off"

    # The one thing that reads a worker while it runs, and the only one.
    assert settings_module.Settings(stack_server=True).tracer_in_force == "parent"
    assert (
        settings_module.Settings(tracer="any", stack_server=True).tracer_in_force == "any"
    )

    # The sampler is not one of them any more: it pushes statuses read from
    # files and asks no worker anything, so a run that only samples declares
    # nothing. This is the assertion that would have to change first if the
    # frames half ever came back.
    assert settings_module.Settings(sample_seconds=5).tracer_in_force == "off"

    # "off" is still "off" where a reader is watching: the escape hatch has to
    # be one for the case it exists for.
    assert settings_module.Settings(tracer="off", stack_server=True).tracer_in_force == "off"


def test_the_worker_is_handed_the_answer_it_has_no_way_to_reach():
    """Where the decision is made, and why it cannot be made at the other end.

    The payload deliberately carries none of the stack server's settings - a
    worker that read them could start its own, which is the collision the
    design exists to prevent - so the question "is anybody going
    to read my stack" is one a worker cannot answer. The controller answers it
    once and sends the answer rather than the evidence for it.
    """
    watched = settings_module.Settings(tracer="any", stack_server=True)
    payload = watched.as_payload()
    assert payload["tracer_handed_down"] == "any"
    assert not [name for name in payload if name.startswith(("stack_server", "sample_"))]

    # The worker obeys it, though everything it can see for itself says "off".
    arrived = settings_module.Settings.from_payload(payload)
    assert arrived.stack_server is False and arrived.sample_seconds == 0.0
    assert arrived.tracer_in_force == "any"

    # And an ordinary run hands down the declining answer just as explicitly.
    quiet = settings_module.Settings(tracer="parent").as_payload()
    assert quiet["tracer_handed_down"] == "off"
    assert settings_module.Settings.from_payload(quiet).tracer_in_force == "off"
