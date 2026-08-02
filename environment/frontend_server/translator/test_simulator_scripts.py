import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase


TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
ACTIVE_SCRIPTS = (
    "home/main_script.html",
    "demo/main_script.html",
    "path_tester/main_script.html",
)


class SimulatorScriptTests(SimpleTestCase):
    def script_source(self, script_path):
        source = (TEMPLATE_DIR / script_path).read_text()
        match = re.search(r"<script[^>]*>(.*?)</script>", source, re.DOTALL)
        self.assertIsNotNone(match, msg=f"No script found in {script_path}")
        return match.group(1)

    def test_active_simulator_scripts_parse(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is required to parse simulator scripts")

        for script_path in ACTIVE_SCRIPTS:
            with self.subTest(script_path=script_path):
                source = re.sub(
                    r"{{.*?}}|{%.*?%}", "0", self.script_source(script_path), flags=re.DOTALL
                )
                with tempfile.NamedTemporaryFile(suffix=".js") as script_file:
                    script_file.write(source.encode())
                    script_file.flush()
                    result = subprocess.run(
                        [node, "--check", script_file.name],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_live_phase_uses_server_recovery_context(self):
        template_source = (TEMPLATE_DIR / "home/main_script.html").read_text()
        source = self.script_source("home/main_script.html")
        self.assertIn('{{ mode|json_script:"simulator-mode" }}', template_source)
        self.assertIn('{{ initial_phase|json_script:"initial-phase" }}', template_source)
        self.assertIn(
            'const simulatorMode = getJsonScriptValue("simulator-mode", "");',
            source,
        )
        self.assertIn(
            'const initialPhase = getJsonScriptValue("initial-phase", "");',
            source,
        )
        self.assertIn(
            'let phase = !initialStepIsValid || step === terminalStep ? "complete" : simulatorMode === "simulate" && (initialPhase === "process" || initialPhase === "update") ? initialPhase : "update";',
            source,
        )

    def test_live_handoff_waits_for_success_and_retries(self):
        source = self.script_source("home/main_script.html")
        post_source = source[
            source.index("async function postEnvironment") : source.index(
                "async function pollMovement"
            )
        ]
        self.assertIn("let environmentPostInFlight = false;", source)
        self.assertIn("await fetch(processEnvironmentUrl", post_source)
        self.assertIn("if (response.status === 503)", post_source)
        self.assertIn("scheduleEnvironmentRetry();", post_source)
        self.assertIn("if (!response.ok)", post_source)
        self.assertIn("if (phase === \"process\" && step === postedStep)", post_source)
        self.assertIn("const retryMaxDelay = 5000;", source)
        self.assertLess(
            post_source.index("if (phase === \"process\" && step === postedStep)"),
            post_source.index('phase = "update";'),
        )
        self.assertIn("Date.now() >= environmentRetryAt", source)

    def test_live_handoff_resyncs_conflicts_once(self):
        source = self.script_source("home/main_script.html")
        post_source = source[
            source.index("async function postEnvironment") : source.index(
                "async function pollMovement"
            )
        ]
        self.assertIn("let resyncInProgress = false;", source)
        self.assertIn("function requestResync()", source)
        self.assertIn("if (resyncInProgress) return;", source)
        self.assertIn("window.location.reload();", source)
        self.assertIn("if (response.status === 409)", post_source)
        self.assertIn("requestResync();", post_source)
        self.assertLess(
            post_source.index("response.status === 409"),
            post_source.index("response.status === 503"),
        )
        self.assertLess(
            post_source.index("response.status === 503"),
            post_source.index("if (!response.ok)"),
        )

    def test_live_movement_poll_retries_without_duplicate_execution(self):
        source = self.script_source("home/main_script.html")
        post_source = source[
            source.index("async function postEnvironment") : source.index(
                "async function pollMovement"
            )
        ]
        poll_source = source[source.index("async function pollMovement") :]
        self.assertIn("let movementPollInFlight = false;", source)
        self.assertIn('let replayComplete = phase === "complete";', source)
        self.assertIn(
            'const replayMovementUrl = "{% url \'replay_movement\' %}";', source
        )
        self.assertIn(
            'const movementUpdateUrl = simulatorMode === "replay" ? replayMovementUrl : updateEnvironmentUrl;',
            source,
        )
        self.assertIn("await fetch(movementUpdateUrl", poll_source)
        self.assertIn('if (simulatorMode !== "simulate") return;', post_source)
        self.assertIn("await response.json();", poll_source)
        self.assertIn("async function pollMovement(pollStep)", poll_source)
        self.assertIn(
            'if (response.status === 409 && simulatorMode === "simulate")', poll_source
        )
        self.assertIn('resync.status === "resync"', poll_source)
        self.assertIn("requestResync();", poll_source)
        self.assertIn("if (response.status === 503)", poll_source)
        self.assertIn("if (!response.ok)", poll_source)
        self.assertIn("if (!isReadyMovement(movement, pollStep))", poll_source)
        self.assertIn(
            'if (simulatorMode === "replay" && movement && movement.status === "complete")',
            poll_source,
        )
        self.assertIn('if (movement["<step>"] !== expectedStep) return false;', source)
        self.assertIn(
            "if (!replayComplete && !movementPollInFlight && Date.now() >= movementPollRetryAt)",
            source,
        )
        self.assertIn("movementPollInFlight = false;", poll_source)
        update_source = source[source.index("function update(time, delta)") :]
        self.assertIn('if (phase == "process" && simulatorMode === "simulate")', update_source)
        self.assertIn(
            'phase = simulatorMode === "replay" ? "update" : "process";', update_source
        )
        self.assertIn("if (!replayComplete && !movementPollInFlight", update_source)
        self.assertLess(
            update_source.index("if (resyncInProgress) return;"),
            update_source.index('if (phase == "process" && simulatorMode === "simulate")'),
        )

    def test_step_contract_uses_safe_integer_terminal_marker(self):
        source = self.script_source("home/main_script.html")
        update_source = source[source.index("function update(time, delta)") :]
        self.assertIn("const terminalStep = Number.MAX_SAFE_INTEGER;", source)
        self.assertIn("const lastExecutableStep = terminalStep - 1;", source)
        self.assertIn("Number.isSafeInteger(parsedInitialStep)", source)
        self.assertIn("if (step === lastExecutableStep)", update_source)
        self.assertIn("step = terminalStep;", update_source)
        self.assertIn("stopSimulator();", update_source)
        self.assertIn("!isExecutableStep(postedStep)", source)
        self.assertIn("!isExecutableStep(pollStep)", source)

    def run_home_script_in_node(self, step, mode, initial_phase, behavior):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is required to run simulator script behavior tests")

        source = re.sub(
            r"{{.*?}}|{%.*?%}", "0", self.script_source("home/main_script.html"), flags=re.DOTALL
        )
        setup = """
const elements = new Map([
  ["step", { textContent: STEP }],
  ["sim_code", { textContent: "test-simulation" }],
  ["persona-init-pos", { textContent: "[]" }],
  ["simulator-mode", { textContent: MODE }],
  ["initial-phase", { textContent: INITIAL_PHASE }],
  ["game-time-content", { textContent: "" }],
  ["play_button", {}],
  ["pause_button", {}]
]);
let reloadCount = 0;
let fetchCount = 0;
globalThis.document = {
  cookie: "",
  getElementById(id) {
    return elements.get(id) || null;
  }
};
globalThis.window = {
  location: {
    reload() {
      reloadCount += 1;
    }
  }
};
globalThis.fetch = async () => {
  fetchCount += 1;
  return { ok: true, status: 200, json: async () => ({}) };
};
globalThis.Phaser = {
  AUTO: 0,
  Game: function Game(config) {
    globalThis.gameConfig = config;
  }
};
"""
        setup = (
            setup.replace("STEP", json.dumps(step))
            .replace("MODE", json.dumps(json.dumps(mode)))
            .replace("INITIAL_PHASE", json.dumps(json.dumps(initial_phase)))
        )
        hooks = """
globalThis.simulatorHooks = {
  prepareLastExecutableMovement() {
    step = Number.MAX_SAFE_INTEGER - 1;
    phase = "execute";
    execute_count = 0;
    movement_target = { Ada: [0, 0] };
    execute_movement = {
      meta: { curr_time: "12:00" },
      persona: {
        Ada: {
          movement: [0, 0],
          pronunciatio: "",
          description: "@",
          chat: null
        }
      }
    };
    personas = {
      Ada: {
        body: { x: 0, y: 0, position: { x: 0, y: 0 } },
        anims: { play() {}, stop() {} },
        setTexture() {}
      }
    };
    pronunciatios = { Ada: { x: 0, y: 0, setText() {} } };
    cursors = {
      left: { isDown: false },
      right: { isDown: false },
      up: { isDown: false },
      down: { isDown: false }
    };
    player = { body: { setVelocity() {} } };
  },
  tick() {
    update.call({ scene: { resume() {}, pause() {} } });
  },
  state() {
    return { step, phase, replayComplete };
  }
};
"""
        program = setup + source + hooks + behavior
        with tempfile.NamedTemporaryFile(suffix=".js") as script_file:
            script_file.write(program.encode())
            script_file.flush()
            result = subprocess.run(
                [node, script_file.name], capture_output=True, text=True, check=False
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_terminal_step_stops_without_environment_posts_or_polls(self):
        for mode in ("simulate", "replay"):
            with self.subTest(mode=mode):
                self.run_home_script_in_node(
                    "9007199254740990",
                    mode,
                    "update",
                    """
simulatorHooks.prepareLastExecutableMovement();
simulatorHooks.tick();
const terminalState = simulatorHooks.state();
if (terminalState.step !== Number.MAX_SAFE_INTEGER) {
  throw new Error("terminal step was not reached");
}
if (terminalState.phase !== "complete" || !terminalState.replayComplete) {
  throw new Error("simulator did not enter complete phase");
}
simulatorHooks.tick();
if (fetchCount !== 0) {
  throw new Error("terminal simulator made a network request");
}
""",
                )

    def test_unsafe_initial_step_resyncs_without_network_requests(self):
        self.run_home_script_in_node(
            "9007199254740992",
            "simulate",
            "process",
            """
simulatorHooks.tick();
const invalidState = simulatorHooks.state();
if (reloadCount !== 1) {
  throw new Error("unsafe initial step did not resync");
}
if (invalidState.phase !== "complete") {
  throw new Error("unsafe initial step did not stop simulator");
}
if (fetchCount !== 0) {
  throw new Error("unsafe initial step made a network request");
}
""",
        )
