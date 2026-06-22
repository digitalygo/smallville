"""
ReverieServer — generative agent simulation backend.

Supports two modes:
  1. Interactive CLI mode (preserved for backwards compatibility)
  2. Service mode (non-interactive, for production deployment)

Service mode starts the simulation backend without stdin prompts and can
continuously process simulation steps as the frontend writes env files.

Usage (interactive):
    uv run python reverie.py

Usage (service mode):
    uv run python reverie.py --serve \\
        --origin base_the_ville_isabella_maria_klaus \\
        --target MySimulation

    # Or via environment variables:
    REVERIE_FORK_SIM_CODE=base_the_ville_isabella_maria_klaus \\
    REVERIE_SIM_CODE=MySimulation \\
        uv run python reverie.py --serve
"""

import argparse
import datetime
import json
import math
import os
import pickle
import shutil
import time
import traceback

from selenium import webdriver

from global_methods import *
from utils import *
from maze import *
from persona.persona import *


##############################################################################
#                                  REVERIE                                   #
##############################################################################


class ReverieServer:
    def __init__(self, fork_sim_code, sim_code, *, resume=False):
        # <sim_code> indicates our current simulation.
        self.sim_code = sim_code
        sim_folder = f"{fs_storage}/{self.sim_code}"

        target_exists = os.path.isdir(sim_folder)

        if resume and target_exists:
            # Resume: target already exists — skip fork, load existing state.
            # Load fork_sim_code from saved meta.json if not provided via CLI.
            print(f"[reverie] Resuming existing simulation: {self.sim_code}")
            with open(f"{sim_folder}/reverie/meta.json") as json_file:
                reverie_meta = json.load(json_file)
            self.fork_sim_code = (
                fork_sim_code or reverie_meta.get("fork_sim_code")
            )
            # Preserve existing metadata — don't overwrite meta.json.
        elif target_exists:
            raise FileExistsError(
                f"Simulation target '{sim_folder}' already exists. "
                f"Use --resume to restart from existing state, or choose a "
                f"different --target name for a fresh fork."
            )
        else:
            # Fresh fork from origin.
            if resume:
                print(
                    f"[reverie] --resume specified but target '{sim_folder}' "
                    f"does not exist. Forking fresh from '{fork_sim_code}'."
                )
            self.fork_sim_code = fork_sim_code
            fork_folder = f"{fs_storage}/{self.fork_sim_code}"
            copyanything(fork_folder, sim_folder)

            with open(f"{sim_folder}/reverie/meta.json") as json_file:
                reverie_meta = json.load(json_file)

            with open(f"{sim_folder}/reverie/meta.json", "w") as outfile:
                reverie_meta["fork_sim_code"] = self.fork_sim_code
                outfile.write(json.dumps(reverie_meta, indent=2))

        # LOADING REVERIE'S GLOBAL VARIABLES
        self.start_time = datetime.datetime.strptime(
            f"{reverie_meta['start_date']}, 00:00:00",
            "%B %d, %Y, %H:%M:%S",
        )
        self.curr_time = datetime.datetime.strptime(
            reverie_meta["curr_time"], "%B %d, %Y, %H:%M:%S"
        )
        self.sec_per_step = reverie_meta["sec_per_step"]

        self.maze = Maze(reverie_meta["maze_name"])
        self.step = reverie_meta["step"]

        # SETTING UP PERSONAS IN REVERIE
        self.personas = dict()
        self.personas_tile = dict()

        init_env_file = f"{sim_folder}/environment/{str(self.step)}.json"
        init_env = json.load(open(init_env_file))
        for persona_name in reverie_meta["persona_names"]:
            persona_folder = f"{sim_folder}/personas/{persona_name}"
            p_x = init_env[persona_name]["x"]
            p_y = init_env[persona_name]["y"]
            curr_persona = Persona(persona_name, persona_folder)

            self.personas[persona_name] = curr_persona
            self.personas_tile[persona_name] = (p_x, p_y)
            self.maze.tiles[p_y][p_x]["events"].add(
                curr_persona.scratch.get_curr_event_and_desc()
            )

        # REVERIE SETTINGS PARAMETERS:
        self.server_sleep = 0.1

        # SIGNALING THE FRONTEND SERVER:
        curr_sim_code = dict()
        curr_sim_code["sim_code"] = self.sim_code
        with open(f"{fs_temp_storage}/curr_sim_code.json", "w") as outfile:
            outfile.write(json.dumps(curr_sim_code, indent=2))

        curr_step = dict()
        curr_step["step"] = self.step
        with open(f"{fs_temp_storage}/curr_step.json", "w") as outfile:
            outfile.write(json.dumps(curr_step, indent=2))

    def save(self):
        """Save all Reverie progress."""
        sim_folder = f"{fs_storage}/{self.sim_code}"

        reverie_meta = dict()
        reverie_meta["fork_sim_code"] = self.fork_sim_code
        reverie_meta["start_date"] = self.start_time.strftime("%B %d, %Y")
        reverie_meta["curr_time"] = self.curr_time.strftime(
            "%B %d, %Y, %H:%M:%S"
        )
        reverie_meta["sec_per_step"] = self.sec_per_step
        reverie_meta["maze_name"] = self.maze.maze_name
        reverie_meta["persona_names"] = list(self.personas.keys())
        reverie_meta["step"] = self.step
        reverie_meta_f = f"{sim_folder}/reverie/meta.json"
        with open(reverie_meta_f, "w") as outfile:
            outfile.write(json.dumps(reverie_meta, indent=2))

        for persona_name, persona in self.personas.items():
            save_folder = (
                f"{sim_folder}/personas/{persona_name}/bootstrap_memory"
            )
            persona.save(save_folder)

    def start_path_tester_server(self):
        """Starts the path tester server."""
        def print_tree(tree):
            def _print_tree(tree, depth):
                dash = " >" * depth

                if type(tree) == type(list()):
                    if tree:
                        print(dash, tree)
                    return

                for key, val in tree.items():
                    if key:
                        print(dash, key)
                    _print_tree(val, depth + 1)

            _print_tree(tree, 0)

        curr_vision = 8
        s_mem = dict()

        while True:
            try:
                curr_dict = {}
                tester_file = fs_temp_storage + "/path_tester_env.json"
                if check_if_file_exists(tester_file):
                    with open(tester_file) as json_file:
                        curr_dict = json.load(json_file)
                        os.remove(tester_file)

                    curr_sts = self.maze.sq_tile_size
                    curr_camera = (
                        int(math.ceil(curr_dict["x"] / curr_sts)),
                        int(math.ceil(curr_dict["y"] / curr_sts)) + 1,
                    )
                    curr_tile_det = self.maze.access_tile(curr_camera)

                    world = curr_tile_det["world"]
                    if curr_tile_det["world"] not in s_mem:
                        s_mem[world] = dict()

                    nearby_tiles = self.maze.get_nearby_tiles(
                        curr_camera, curr_vision
                    )
                    for i in nearby_tiles:
                        i_det = self.maze.access_tile(i)
                        if (
                            curr_tile_det["sector"] == i_det["sector"]
                            and curr_tile_det["arena"] == i_det["arena"]
                        ):
                            if i_det["sector"] != "":
                                if i_det["sector"] not in s_mem[world]:
                                    s_mem[world][i_det["sector"]] = dict()
                            if i_det["arena"] != "":
                                if (
                                    i_det["arena"]
                                    not in s_mem[world][i_det["sector"]]
                                ):
                                    s_mem[world][i_det["sector"]][
                                        i_det["arena"]
                                    ] = list()
                            if i_det["game_object"] != "":
                                if (
                                    i_det["game_object"]
                                    not in s_mem[world][i_det["sector"]][
                                        i_det["arena"]
                                    ]
                                ):
                                    s_mem[world][i_det["sector"]][
                                        i_det["arena"]
                                    ] += [i_det["game_object"]]

                print("= " * 15)
                out_file = fs_temp_storage + "/path_tester_out.json"
                with open(out_file, "w") as outfile:
                    outfile.write(json.dumps(s_mem, indent=2))
                print_tree(s_mem)

            except:
                pass

            time.sleep(self.server_sleep * 10)

    def start_server(self, int_counter):
        """The main backend server of Reverie."""
        sim_folder = f"{fs_storage}/{self.sim_code}"

        game_obj_cleanup = dict()

        while True:
            if int_counter == 0:
                break

            curr_env_file = f"{sim_folder}/environment/{self.step}.json"
            if check_if_file_exists(curr_env_file):
                try:
                    with open(curr_env_file) as json_file:
                        new_env = json.load(json_file)
                        env_retrieved = True
                except:
                    pass

                if env_retrieved:
                    for key, val in game_obj_cleanup.items():
                        self.maze.turn_event_from_tile_idle(key, val)
                    game_obj_cleanup = dict()

                    for persona_name, persona in self.personas.items():
                        curr_tile = self.personas_tile[persona_name]
                        new_tile = (
                            new_env[persona_name]["x"],
                            new_env[persona_name]["y"],
                        )

                        self.personas_tile[persona_name] = new_tile
                        self.maze.remove_subject_events_from_tile(
                            persona.name, curr_tile
                        )
                        self.maze.add_event_from_tile(
                            persona.scratch.get_curr_event_and_desc(), new_tile
                        )

                        if not persona.scratch.planned_path:
                            game_obj_cleanup[
                                persona.scratch.get_curr_obj_event_and_desc()
                            ] = new_tile
                            self.maze.add_event_from_tile(
                                persona.scratch.get_curr_obj_event_and_desc(),
                                new_tile,
                            )
                            blank = (
                                persona.scratch.get_curr_obj_event_and_desc()[0],
                                None,
                                None,
                                None,
                            )
                            self.maze.remove_event_from_tile(blank, new_tile)

                    movements = {"persona": dict(), "meta": dict()}
                    for persona_name, persona in self.personas.items():
                        next_tile, pronunciatio, description = persona.move(
                            self.maze,
                            self.personas,
                            self.personas_tile[persona_name],
                            self.curr_time,
                        )
                        movements["persona"][persona_name] = {}
                        movements["persona"][persona_name][
                            "movement"
                        ] = next_tile
                        movements["persona"][persona_name][
                            "pronunciatio"
                        ] = pronunciatio
                        movements["persona"][persona_name][
                            "description"
                        ] = description
                        movements["persona"][persona_name][
                            "chat"
                        ] = persona.scratch.chat

                    movements["meta"]["curr_time"] = self.curr_time.strftime(
                        "%B %d, %Y, %H:%M:%S"
                    )

                    curr_move_file = (
                        f"{sim_folder}/movement/{self.step}.json"
                    )
                    with open(curr_move_file, "w") as outfile:
                        outfile.write(json.dumps(movements, indent=2))

                    self.step += 1
                    self.curr_time += datetime.timedelta(
                        seconds=self.sec_per_step
                    )

                    int_counter -= 1

            time.sleep(self.server_sleep)

    def serve_forever(self, autosave_interval: int = 10):
        """
        Service mode: continuously process simulation steps as the frontend
        writes env files. Autosaves every N steps.

        Blocks until interrupted.
        """
        sim_folder = f"{fs_storage}/{self.sim_code}"
        game_obj_cleanup = dict()
        steps_since_save = 0
        step_count = 0

        print(f"[reverie] Service mode started for sim_code={self.sim_code}")
        print(f"[reverie] Watching {sim_folder}/environment/")
        print(f"[reverie] Autosave every {autosave_interval} steps")
        print(f"[reverie] Press Ctrl+C to stop")

        try:
            while True:
                curr_env_file = f"{sim_folder}/environment/{self.step}.json"
                if check_if_file_exists(curr_env_file):
                    env_retrieved = False
                    try:
                        with open(curr_env_file) as json_file:
                            new_env = json.load(json_file)
                            env_retrieved = True
                    except Exception:
                        pass

                    if env_retrieved:
                        for key, val in game_obj_cleanup.items():
                            self.maze.turn_event_from_tile_idle(key, val)
                        game_obj_cleanup = dict()

                        for persona_name, persona in self.personas.items():
                            curr_tile = self.personas_tile[persona_name]
                            new_tile = (
                                new_env[persona_name]["x"],
                                new_env[persona_name]["y"],
                            )

                            self.personas_tile[persona_name] = new_tile
                            self.maze.remove_subject_events_from_tile(
                                persona.name, curr_tile
                            )
                            self.maze.add_event_from_tile(
                                persona.scratch.get_curr_event_and_desc(),
                                new_tile,
                            )

                            if not persona.scratch.planned_path:
                                game_obj_cleanup[
                                    persona.scratch.get_curr_obj_event_and_desc()
                                ] = new_tile
                                self.maze.add_event_from_tile(
                                    persona.scratch.get_curr_obj_event_and_desc(),
                                    new_tile,
                                )
                                blank = (
                                    persona.scratch.get_curr_obj_event_and_desc()[0],
                                    None,
                                    None,
                                    None,
                                )
                                self.maze.remove_event_from_tile(
                                    blank, new_tile
                                )

                        movements = {"persona": dict(), "meta": dict()}
                        for persona_name, persona in self.personas.items():
                            (
                                next_tile,
                                pronunciatio,
                                description,
                            ) = persona.move(
                                self.maze,
                                self.personas,
                                self.personas_tile[persona_name],
                                self.curr_time,
                            )
                            movements["persona"][persona_name] = {}
                            movements["persona"][persona_name][
                                "movement"
                            ] = next_tile
                            movements["persona"][persona_name][
                                "pronunciatio"
                            ] = pronunciatio
                            movements["persona"][persona_name][
                                "description"
                            ] = description
                            movements["persona"][persona_name][
                                "chat"
                            ] = persona.scratch.chat

                        movements["meta"][
                            "curr_time"
                        ] = self.curr_time.strftime(
                            "%B %d, %Y, %H:%M:%S"
                        )

                        curr_move_file = (
                            f"{sim_folder}/movement/{self.step}.json"
                        )
                        with open(curr_move_file, "w") as outfile:
                            outfile.write(json.dumps(movements, indent=2))

                        self.step += 1
                        self.curr_time += datetime.timedelta(
                            seconds=self.sec_per_step
                        )
                        step_count += 1
                        steps_since_save += 1

                        if steps_since_save >= autosave_interval:
                            self.save()
                            steps_since_save = 0
                            print(
                                f"[reverie] Autosaved at step {self.step} "
                                f"({step_count} steps processed)"
                            )

                time.sleep(self.server_sleep)

        except KeyboardInterrupt:
            print(f"\n[reverie] Received interrupt. Saving and exiting...")
            self.save()
            print(f"[reverie] Saved at step {self.step}. Goodbye.")

    def open_server(self):
        """Open up an interactive terminal prompt for running the simulation."""
        print(
            "Note: The agents in this simulation package are computational"
        )
        print(
            "constructs powered by generative agents architecture and LLM. We"
        )
        print(
            "clarify that these agents lack human-like agency, consciousness,"
        )
        print("and independent decision-making.\n---")

        sim_folder = f"{fs_storage}/{self.sim_code}"

        while True:
            sim_command = input("Enter option: ")
            sim_command = sim_command.strip()
            ret_str = ""

            try:
                if sim_command.lower() in [
                    "f",
                    "fin",
                    "finish",
                    "save and finish",
                ]:
                    self.save()
                    break

                elif sim_command.lower() == "start path tester mode":
                    shutil.rmtree(sim_folder)
                    self.start_path_tester_server()

                elif sim_command.lower() == "exit":
                    shutil.rmtree(sim_folder)
                    break

                elif sim_command.lower() == "save":
                    self.save()

                elif sim_command[:3].lower() == "run":
                    int_count = int(sim_command.split()[-1])
                    rs.start_server(int_count)

                elif "print persona schedule" in sim_command[:22].lower():
                    ret_str += self.personas[
                        " ".join(sim_command.split()[-2:])
                    ].scratch.get_str_daily_schedule_summary()

                elif "print all persona schedule" in sim_command[:26].lower():
                    for persona_name, persona in self.personas.items():
                        ret_str += f"{persona_name}\n"
                        ret_str += f"{persona.scratch.get_str_daily_schedule_summary()}\n"
                        ret_str += f"---\n"

                elif "print hourly org persona schedule" in sim_command.lower():
                    ret_str += self.personas[
                        " ".join(sim_command.split()[-2:])
                    ].scratch.get_str_daily_schedule_hourly_org_summary()

                elif "print persona current tile" in sim_command[:26].lower():
                    ret_str += str(
                        self.personas[
                            " ".join(sim_command.split()[-2:])
                        ].scratch.curr_tile
                    )

                elif "print persona chatting with buffer" in sim_command.lower():
                    curr_persona = self.personas[
                        " ".join(sim_command.split()[-2:])
                    ]
                    for (
                        p_n,
                        count,
                    ) in curr_persona.scratch.chatting_with_buffer.items():
                        ret_str += f"{p_n}: {count}"

                elif (
                    "print persona associative memory (event)"
                    in sim_command.lower()
                ):
                    ret_str += (
                        f'{self.personas[" ".join(sim_command.split()[-2:])]}\n'
                    )
                    ret_str += self.personas[
                        " ".join(sim_command.split()[-2:])
                    ].a_mem.get_str_seq_events()

                elif (
                    "print persona associative memory (thought)"
                    in sim_command.lower()
                ):
                    ret_str += (
                        f'{self.personas[" ".join(sim_command.split()[-2:])]}\n'
                    )
                    ret_str += self.personas[
                        " ".join(sim_command.split()[-2:])
                    ].a_mem.get_str_seq_thoughts()

                elif (
                    "print persona associative memory (chat)"
                    in sim_command.lower()
                ):
                    ret_str += (
                        f'{self.personas[" ".join(sim_command.split()[-2:])]}\n'
                    )
                    ret_str += self.personas[
                        " ".join(sim_command.split()[-2:])
                    ].a_mem.get_str_seq_chats()

                elif "print persona spatial memory" in sim_command.lower():
                    self.personas[
                        " ".join(sim_command.split()[-2:])
                    ].s_mem.print_tree()

                elif "print current time" in sim_command[:18].lower():
                    ret_str += (
                        f'{self.curr_time.strftime("%B %d, %Y, %H:%M:%S")}\n'
                    )
                    ret_str += f"steps: {self.step}"

                elif "print tile event" in sim_command[:16].lower():
                    cooordinate = [
                        int(i.strip()) for i in sim_command[16:].split(",")
                    ]
                    for i in self.maze.access_tile(cooordinate)["events"]:
                        ret_str += f"{i}\n"

                elif "print tile details" in sim_command.lower():
                    cooordinate = [
                        int(i.strip()) for i in sim_command[18:].split(",")
                    ]
                    for key, val in self.maze.access_tile(cooordinate).items():
                        ret_str += f"{key}: {val}\n"

                elif "call -- analysis" in sim_command.lower():
                    persona_name = sim_command[
                        len("call -- analysis") :
                    ].strip()
                    self.personas[persona_name].open_convo_session("analysis")

                elif "call -- load history" in sim_command.lower():
                    curr_file = (
                        maze_assets_loc
                        + "/"
                        + sim_command[len("call -- load history") :].strip()
                    )

                    rows = read_file_to_list(
                        curr_file, header=True, strip_trail=True
                    )[1]
                    clean_whispers = []
                    for row in rows:
                        agent_name = row[0].strip()
                        whispers = row[1].split(";")
                        whispers = [whisper.strip() for whisper in whispers]
                        for whisper in whispers:
                            clean_whispers += [[agent_name, whisper]]

                    load_history_via_whisper(self.personas, clean_whispers)

                print(ret_str)

            except:
                traceback.print_exc()
                print("Error.")
                pass


##############################################################################
#                                  MAIN CLI                                  #
##############################################################################


def _parse_args():
    parser = argparse.ArgumentParser(
        description="ReverieServer — generative agent simulation backend",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run in non-interactive service mode (production).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing simulation instead of forking fresh.",
    )
    parser.add_argument(
        "--origin",
        dest="fork_sim_code",
        help="Name of the forked simulation (overrides REVERIE_FORK_SIM_CODE).",
    )
    parser.add_argument(
        "--target",
        dest="sim_code",
        help="Name of the new simulation (overrides REVERIE_SIM_CODE).",
    )
    parser.add_argument(
        "--autosave-interval",
        type=int,
        default=10,
        help="Autosave every N steps in service mode (default: 10).",
    )
    return parser.parse_args()


def _resolve_codes(args):
    """Resolve simulation codes from args or env vars."""
    fork_sim_code = args.fork_sim_code or os.getenv("REVERIE_FORK_SIM_CODE")
    sim_code = args.sim_code or os.getenv("REVERIE_SIM_CODE")

    if not sim_code:
        # Interactive fallback — prompt for codes
        sim_code = input("Enter the name of the new simulation: ").strip()

    # When resuming an existing simulation, origin is not required —
    # the fork_sim_code will be loaded from the saved meta.json on disk.
    target_exists = sim_code and os.path.isdir(f"{fs_storage}/{sim_code}")
    if args.resume and target_exists:
        if not fork_sim_code and not sim_code:
            fork_sim_code = input(
                "Enter the name of the forked simulation: "
            ).strip()
    elif not fork_sim_code:
        fork_sim_code = input(
            "Enter the name of the forked simulation: "
        ).strip()

    return fork_sim_code, sim_code


if __name__ == "__main__":
    # Ensure we start from the right working directory (reverie/backend_server/)
    # so that the relative paths in utils.py (fs_storage, maze_assets_loc) resolve.
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(_script_dir)

    args = _parse_args()

    if args.serve:
        # Non-interactive service mode
        fork_sim_code, sim_code = _resolve_codes(args)
        if args.resume:
            print(f"[reverie] Resuming simulation: {sim_code}")
        else:
            print(f"[reverie] Forking from: {fork_sim_code}")
            print(f"[reverie] Creating new simulation: {sim_code}")
        rs = ReverieServer(fork_sim_code, sim_code, resume=args.resume)
        rs.serve_forever(autosave_interval=args.autosave_interval)
    else:
        # Interactive CLI mode (backwards compatible)
        fork_sim_code, sim_code = _resolve_codes(args)
        print(f"Forking from: {fork_sim_code}")
        print(f"Creating new simulation: {sim_code}")
        rs = ReverieServer(fork_sim_code, sim_code)
        rs.open_server()
