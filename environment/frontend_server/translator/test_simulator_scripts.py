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

    def test_live_template_carries_simulator_target_fps_bridge(self):
        template_source = (TEMPLATE_DIR / "home/main_script.html").read_text()
        source = self.script_source("home/main_script.html")
        self.assertIn(
            '{{ simulator_target_fps|json_script:"simulator-target-fps" }}',
            template_source,
        )
        self.assertIn(
            'const simulatorTargetFps = getJsonScriptValue("simulator-target-fps", 30);',
            source,
        )
        self.assertIn("fps: { target: simulatorTargetFps },", source)

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

    def run_home_script_in_node(self, step, mode, initial_phase, behavior, fps_json=None):
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
FPS_ELEMENT
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
        if fps_json is None:
            setup = setup.replace("FPS_ELEMENT\n", "")
        else:
            setup = setup.replace(
                "FPS_ELEMENT",
                f'  ["simulator-target-fps", {{ textContent: {json.dumps(fps_json)} }}],',
            )
        hooks = """
globalThis.simulatorHooks = {
  prepareLastExecutableMovement() {
    step = Number.MAX_SAFE_INTEGER - 1;
    phase = "execute";
    motionElapsed = 650;
    motionStarted = false;
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
        x: 16, y: 32,
        setPosition(x, y) { this.x = x; this.y = y; },
        setFrame() {},
        anims: { play() {}, stop() {} },
        setTexture() {}
      }
    };
    pronunciatios = { Ada: { x: 0, y: 0, setText() {}, setPosition() {} } };
    cursors = {
      left: { isDown: false },
      right: { isDown: false },
      up: { isDown: false },
      down: { isDown: false }
    };
    player = { body: { setVelocity() {} } };
  },
  tick(delta=100) {
    update.call({ scene: { resume() {}, pause() {} } }, 0, delta);
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

    def test_game_config_fps_target_uses_injected_setting(self):
        self.run_home_script_in_node(
            "0",
            "simulate",
            "update",
            """
if (!globalThis.gameConfig || !globalThis.gameConfig.fps || globalThis.gameConfig.fps.target !== 60) {
  throw new Error("game config fps.target did not use the injected setting");
}
""",
            fps_json="60",
        )

    def test_game_config_fps_target_defaults_to_30(self):
        self.run_home_script_in_node(
            "0",
            "simulate",
            "update",
            """
if (!globalThis.gameConfig || !globalThis.gameConfig.fps || globalThis.gameConfig.fps.target !== 30) {
  throw new Error("game config fps.target did not fall back to 30");
}
""",
        )

    def test_smooth_movement_commits_all_logical_tiles_once_before_next_post(self):
        self.run_home_script_in_node("0", "simulate", "update", """
(async () => {
  simulatorHooks.prepareLastExecutableMovement();
  step = 0;
  phase = "update";
  motionElapsed = 0;
  logicalPositions = { Ada: [0,0], Grace: [2,0] };
  personas.Grace = { ...personas.Ada, x:80, y:32 };
  pronunciatios.Grace = { setText() {}, setPosition() {} };
  let calls = [];
  const ready = {"<step>":0, meta:{curr_time:"09:00:00"}, persona:{
    Ada:{movement:[1,0],pronunciatio:"walk",description:"walking @ cafe",chat:null},
    Grace:{movement:[2,0],pronunciatio:"read",description:"reading @ home",chat:null}
  }};
  globalThis.fetch = async (url, options) => {
    calls.push(JSON.parse(options.body));
    return {ok:true,status:200,json:async()=>ready};
  };
  await pollMovement(0);
  if (phase !== "execute") throw new Error("ready movement was not accepted");
  simulatorHooks.tick(50);
  if (personas.Ada.x <= 16 || personas.Ada.x >= 48) throw new Error("motion did not interpolate");
  if (logicalPositions.Ada[0] !== 0 || step !== 0) throw new Error("logical step committed before motion completed");
  paused = true;
  const frozen = personas.Ada.x;
  simulatorHooks.tick(100);
  if (personas.Ada.x !== frozen) throw new Error("pause did not stop motion");
  paused = false;
  for (let i=0;i<12;i++) simulatorHooks.tick(50);
  if (step !== 1 || phase !== "process") throw new Error("multi-resident step did not commit exactly once");
  if (logicalPositions.Ada[0] !== 1 || logicalPositions.Grace[0] !== 2) throw new Error("logical tiles were not committed");
  if (personas.Ada.x !== 48 || personas.Grace.x !== 80) throw new Error("sprite endpoints drifted");
  simulatorHooks.tick(16);
  await new Promise(resolve=>setImmediate(resolve));
  const posted = calls.at(-1);
  if (posted.step !== 1 || posted.environment.Ada.x !== 1 || posted.environment.Grace.x !== 2) throw new Error("next environment did not use committed logical tiles");
  if (phase !== "update") throw new Error("environment acknowledgement did not advance phase");
})().catch(error=>{ console.error(error); process.exitCode=1; });
""")

    def test_pending_and_wrong_step_do_not_change_positions_or_execute(self):
        self.run_home_script_in_node("0", "simulate", "update", """
(async () => {
  simulatorHooks.prepareLastExecutableMovement();
  step = 0; phase = "update";
  const initialX = personas.Ada.x;
  globalThis.fetch = async () => ({ok:true,status:202,json:async()=>({status:"pending"})});
  await pollMovement(0);
  if (phase !== "update" || step !== 0 || personas.Ada.x !== initialX) throw new Error("pending response advanced the simulation");
  globalThis.fetch = async () => ({ok:true,status:200,json:async()=>({"<step>":1,meta:{curr_time:"09:00"},persona:{Ada:{movement:[1,0],pronunciatio:"",description:"walk",chat:null}}})});
  await pollMovement(0);
  if (phase !== "update" || step !== 0 || personas.Ada.x !== initialX) throw new Error("wrong-step movement was accepted");
})().catch(error=>{ console.error(error); process.exitCode=1; });
""")

    def test_reduced_motion_commits_the_same_step_without_interpolation(self):
        self.run_home_script_in_node("0", "replay", "update", """
simulatorHooks.prepareLastExecutableMovement();
step=0; motionElapsed=0;
execute_movement.persona.Ada.movement=[1,0];
window.matchMedia=()=>({matches:true});
simulatorHooks.tick(16);
if (step !== 1 || phase !== "update" || logicalPositions.Ada[0] !== 1 || personas.Ada.x !== 48) throw new Error("reduced motion changed the logical result");
""")

    def test_expired_session_stops_without_committing_a_step(self):
        self.run_home_script_in_node("0", "simulate", "update", """
(async () => {
  globalThis.fetch=async()=>({ok:false,status:403});
  await pollMovement(0);
  if (phase !== "complete" || step !== 0 || !statusMessage.includes("sign in")) throw new Error("expired session did not stop with recovery guidance");
})().catch(error=>{ console.error(error); process.exitCode=1; });
""")
