# Generative Agents: Interactive Simulacra of Human Behavior 

<p align="center" width="100%">
<img src="cover.png" alt="Smallville" style="width: 80%; min-width: 300px; display: block; margin: auto;">
</p>

This repository accompanies our research paper titled "[Generative Agents: Interactive Simulacra of Human Behavior](https://arxiv.org/abs/2304.03442)." It contains our core simulation module for generative agents—computational agents that simulate believable human behaviors—and their game environment. Below, we document the steps for setting up the simulation environment on your local machine and for replaying the simulation as a demo animation.

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Isabella_Rodriguez.png" alt="Generative Isabella">   Setting up the environment

This project uses [uv](https://docs.astral.sh/uv/) for dependency management and requires **Python 3.12+**.

### Step 1. Install dependencies

```bash
uv sync
```

This installs all packages declared in `pyproject.toml` (with locked versions from `uv.lock`) into a virtual environment managed by uv.

If you prefer a standard virtualenv, a legacy `requirements.txt` is still present at the repo root, but `uv sync` is the recommended approach.

### Step 2. Configure environment variables

```bash
cp .env.example .env
```

Then edit `.env` with your API keys and preferences. The configuration file (`reverie/backend_server/utils.py`) loads all settings from `.env` at runtime—no code changes needed.

See the next section for provider and model options. 

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Klaus_Mueller.png" alt="Generative Klaus">   LLM provider configuration

Smallville supports two providers: **OpenRouter** (recommended) and **OpenAI**.

### Provider selection

Set `LLM_PROVIDER` in your `.env`:

```env
LLM_PROVIDER=openrouter    # or "openai"
```

When `LLM_PROVIDER` is unset, the code falls back to `openai`.

### API keys

- **OpenRouter**: set `OPENROUTER_API_KEY`
- **OpenAI**: set `OPENAI_API_KEY`

You only need the key for the provider you select.

### Default model

Set `LLM_MODEL` to override the provider-specific default:

| Provider | Default model |
|----------|---------------|
| OpenAI | `gpt-4o` |
| OpenRouter | `openai/gpt-4o` |

Any model slug your provider supports is valid (e.g., `anthropic/claude-3.5-sonnet` on OpenRouter).

### OpenRouter attribution

When using OpenRouter, the following `.env` variables are sent as attribution headers:

```env
OPENROUTER_REFERER=https://github.com/digitalygo/smallville
OPENROUTER_TITLE=smallville
OPENROUTER_CATEGORIES=game,roleplay
```

### Per-persona model assignment

You can assign different models to individual personas. Two mechanisms exist, applied in priority order:

1.  **scratch.json fields** — add optional `"llm_provider"` and `"llm_model"` fields to a persona's bootstrap `scratch.json` file.
2.  **`LLM_PERSONA_MODELS` in `.env`** — a JSON map keyed by persona name.

Example using `LLM_PERSONA_MODELS`:

```env
LLM_PERSONA_MODELS='{"Isabella Rodriguez":"openrouter:anthropic/claude-3.5-sonnet","Maria Lopez":"openai:gpt-4o"}'
```

Prefix a value with `openrouter:` or `openai:` to set both provider and model. A bare model name (e.g., `"gpt-4o"`) uses the current provider. If no override is set for a persona, the global defaults above apply.

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Klaus_Mueller.png" alt="Generative Klaus">   Operator authentication

Simulation control pages (`/simulator_home`, `/path_tester/`) and POST endpoints require authentication. Create a Django superuser account:

```bash
cd environment/frontend_server
uv run python manage.py createsuperuser
```

Then log in at `/accounts/login/` before accessing simulation controls. This is enforced in all environments (dev and production).

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Klaus_Mueller.png" alt="Generative Klaus">   Running a Simulation 
To run a new simulation, you will need to concurrently start two servers: the environment server and the agent simulation server.

### Step 1. Starting the Environment Server
Again, the environment is implemented as a Django project, and as such, you will need to start the Django server. To do this, first navigate to `environment/frontend_server` (this is where `manage.py` is located) in your command line. Then run the following command:

    uv run python manage.py runserver

> **Note:** The Django stack has been production-hardened with proper CSRF protection, operator authentication, input validation, and WhiteNoise static file serving. For production deployment, use `--settings=frontend_server.settings.production` with `gunicorn` (see the included `Procfile`). Run `uv run python manage.py collectstatic --no-input` first to populate the WhiteNoise static cache.

Then, on your favorite browser, go to [http://localhost:8000/](http://localhost:8000/). If you see a message that says, "Your environment server is up and running," your server is running properly. Ensure that the environment server continues to run while you are running the simulation, so keep this command-line tab open! (Note: I recommend using either Chrome or Safari. Firefox might produce some frontend glitches, although it should not interfere with the actual simulation.)

### Step 2. Starting the Simulation Server
Open up another command line (the one you used in Step 1 should still be running the environment server, so leave that as it is). Navigate to `reverie/backend_server` and run `reverie.py`.

**Interactive mode** (original, with CLI prompts):

    uv run python reverie.py

This will start the simulation server. A command-line prompt will appear, asking the following: "Enter the name of the forked simulation: ". To start a 3-agent simulation with Isabella Rodriguez, Maria Lopez, and Klaus Mueller, type the following:
    
    base_the_ville_isabella_maria_klaus

The prompt will then ask, "Enter the name of the new simulation: ". Type any name to denote your current simulation (e.g., just "test-simulation" will do for now).

    test-simulation

Keep the simulator server running. At this stage, it will display the following prompt: "Enter option: "

**Service mode** (non-interactive, for production deployment):

    uv run python reverie.py --serve \\
        --origin base_the_ville_isabella_maria_klaus \\
        --target MySimulation

This starts the backend in a non-interactive loop that processes simulation steps as the frontend writes environment files. The service autosaves every 10 steps and exits cleanly on `Ctrl+C`. To resume a previously saved simulation without re-forking:

    uv run python reverie.py --serve --resume \\
        --target MySimulation

### Step 3. Running and Saving the Simulation
On your browser, navigate to [http://localhost:8000/simulator_home](http://localhost:8000/simulator_home). You should see the map of Smallville, along with a list of active agents on the map. You can move around the map using your keyboard arrows. Please keep this tab open. To run the simulation, type the following command in your simulation server in response to the prompt, "Enter option":

    run <step-count>
Note that you will want to replace `<step-count>` above with an integer indicating the number of game steps you want to simulate. For instance, if you want to simulate 100 game steps, you should input `run 100`. One game step represents 10 seconds in the game.


Your simulation should be running, and you will see the agents moving on the map in your browser. Once the simulation finishes running, the "Enter option" prompt will re-appear. At this point, you can simulate more steps by re-entering the run command with your desired game steps, exit the simulation without saving by typing `exit`, or save and exit by typing `fin`.

The saved simulation can be accessed the next time you run the simulation server by providing the name of your simulation as the forked simulation. This will allow you to restart your simulation from the point where you left off.

### Step 4. Replaying a Simulation
You can replay a simulation that you have already run simply by having your environment server running and navigating to the following address in your browser: `http://localhost:8000/replay/<simulation-name>/<starting-time-step>`. Please make sure to replace `<simulation-name>` with the name of the simulation you want to replay, and `<starting-time-step>` with the integer time-step from which you wish to start the replay.

For instance, by visiting the following link, you will initiate a pre-simulated example, starting at time-step 1:  
[http://localhost:8000/replay/July1_the_ville_isabella_maria_klaus-step-3-20/1/](http://localhost:8000/replay/July1_the_ville_isabella_maria_klaus-step-3-20/1/)

### Step 5. Demoing a Simulation
You may have noticed that all character sprites in the replay look identical. We would like to clarify that the replay function is primarily intended for debugging purposes and does not prioritize optimizing the size of the simulation folder or the visuals. To properly demonstrate a simulation with appropriate character sprites, you will need to compress the simulation first. To do this, open the `compress_sim_storage.py` file located in the `reverie` directory using a text editor. Then, execute the `compress` function with the name of the target simulation as its input. By doing so, the simulation file will be compressed, making it ready for demonstration.

To start the demo, go to the following address on your browser: `http://localhost:8000/demo/<simulation-name>/<starting-time-step>/<simulation-speed>`. Note that `<simulation-name>` and `<starting-time-step>` denote the same things as mentioned above. `<simulation-speed>` can be set to control the demo speed, where 1 is the slowest, and 5 is the fastest. For instance, visiting the following link will start a pre-simulated example, beginning at time-step 1, with a medium demo speed:  
[http://localhost:8000/demo/July1_the_ville_isabella_maria_klaus-step-3-20/1/3/](http://localhost:8000/demo/July1_the_ville_isabella_maria_klaus-step-3-20/1/3/)

### Tips
We've noticed that OpenAI's API can hang when it reaches the hourly rate limit. When this happens, you may need to restart your simulation. For now, we recommend saving your simulation often as you progress to ensure that you lose as little of the simulation as possible when you do need to stop and rerun it. Running these simulations, at least as of early 2023, could be somewhat costly, especially when there are many agents in the environment.

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Isabella_Rodriguez.png" alt="Generative Isabella">   Production deployment (Docker Compose)

The repo includes a containerized production stack (`web` + `backend` + `db`).

### Prerequisites

- Docker Engine and Compose Plugin v2 installed.
- At minimum these `.env` vars set (copy from `.env.example` first):
  - `DJANGO_SECRET_KEY` — a random 50+ char string
  - `OPENROUTER_API_KEY` (or `OPENAI_API_KEY`)
  - `REVERIE_SIM_CODE` — name for the simulation (e.g. `MySimulation`)
  - `REVERIE_FORK_SIM_CODE` — base fork name, only needed for the first run (e.g. `base_the_ville_isabella_maria_klaus`)

```bash
cp .env.example .env
# then edit .env with your values
```

### Build and start

```bash
docker compose -f docker-compose.prod.yml up --build -d
```

This builds the `smallville:latest` image once, then starts three services:

| Service | What it runs |
|---------|--------------|
| `web` | gunicorn (`frontend_server.wsgi`) with Django production settings, bound to `:8000` |
| `backend` | `reverie.py --serve --resume` — autosaves every 10 steps |
| `db` | PostgreSQL 16, auto-connected via `DATABASE_URL` |

The web service is exposed on `${WEB_PORT:-8000}`. The backend and db communicate on an internal bridge network.

### First-login and superuser account

Navigate to `http://localhost:8000/accounts/login/`, then create a superuser inside the running container:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser
```

Log in with those credentials to access simulation controls (`/simulator_home`, `/path_tester/`).

### Shared simulation storage

The `web` and `backend` services share the same simulation storage through Docker volumes — specifically `smallville_storage` (simulations) and `smallville_temp_storage` (runtime state). The frontend writes environment files that the backend reads each step, then writes its output back for the frontend to render.

### HTTPS / reverse proxy

The stack enables `DJANGO_SECURE_SSL_REDIRECT=true` by default (secure). When running without a TLS-terminating reverse proxy, override in `.env`:

```bash
DJANGO_SECURE_SSL_REDIRECT=false
```

For production expose, place a reverse proxy (nginx, Caddy, Traefik) in front of the `web` service to handle TLS termination and keep the default SSL redirect on.

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Maria_Lopez.png" alt="Generative Maria">   Simulation Storage Location
All simulations that you save will be located in `environment/frontend_server/storage`, and all compressed demos will be located in `environment/frontend_server/compressed_storage`. 

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Sam_Moore.png" alt="Generative Sam">   Customization

There are two ways to optionally customize your simulations. 

### Author and Load Agent History
First is to initialize agents with unique history at the start of the simulation. To do this, you would want to 1) start your simulation using one of the base simulations, and 2) author and load agent history. More specifically, here are the steps:

#### Step 1. Starting Up a Base Simulation 
There are two base simulations included in the repository: `base_the_ville_n25` with 25 agents, and `base_the_ville_isabella_maria_klaus` with 3 agents. Load one of the base simulations by following the steps until step 2 above. 

#### Step 2. Loading a History File 
Then, when prompted with "Enter option: ", you should load the agent history by responding with the following command:

    call -- load history the_ville/<history_file_name>.csv
Note that you will need to replace `<history_file_name>` with the name of an existing history file. There are two history files included in the repo as examples: `agent_history_init_n25.csv` for `base_the_ville_n25` and `agent_history_init_n3.csv` for `base_the_ville_isabella_maria_klaus`. These files include semicolon-separated lists of memory records for each of the agents—loading them will insert the memory records into the agents' memory stream.

#### Step 3. Further Customization 
To customize the initialization by authoring your own history file, place your file in the following folder: `environment/frontend_server/static_dirs/assets/the_ville`. The column format for your custom history file will have to match the example history files included. Therefore, we recommend starting the process by copying and pasting the ones that are already in the repository.

### Create New Base Simulations
For a more involved customization, you will need to author your own base simulation files. The most straightforward approach would be to copy and paste an existing base simulation folder, renaming and editing it according to your requirements. This process will be simpler if you decide to keep the agent names unchanged. However, if you wish to change their names or increase the number of agents that the Smallville map can accommodate, you might need to directly edit the map using the [Tiled](https://www.mapeditor.org/) map editor.


## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Eddy_Lin.png" alt="Generative Eddy">   Authors and Citation 

**Authors:** Joon Sung Park, Joseph C. O'Brien, Carrie J. Cai, Meredith Ringel Morris, Percy Liang, Michael S. Bernstein

Please cite our paper if you use the code or data in this repository. 
```
@inproceedings{Park2023GenerativeAgents,  
author = {Park, Joon Sung and O'Brien, Joseph C. and Cai, Carrie J. and Morris, Meredith Ringel and Liang, Percy and Bernstein, Michael S.},  
title = {Generative Agents: Interactive Simulacra of Human Behavior},  
year = {2023},  
publisher = {Association for Computing Machinery},  
address = {New York, NY, USA},  
booktitle = {In the 36th Annual ACM Symposium on User Interface Software and Technology (UIST '23)},  
keywords = {Human-AI interaction, agents, generative AI, large language models},  
location = {San Francisco, CA, USA},  
series = {UIST '23}
}
```

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Wolfgang_Schulz.png" alt="Generative Wolfgang">   Acknowledgements

We encourage you to support the following three amazing artists who have designed the game assets for this project, especially if you are planning to use the assets included here for your own project: 
* Background art: [PixyMoon (@_PixyMoon\_)](https://twitter.com/_PixyMoon_)
* Furniture/interior design: [LimeZu (@lime_px)](https://twitter.com/lime_px)
* Character design: [ぴぽ (@pipohi)](https://twitter.com/pipohi)

In addition, we thank Lindsay Popowski, Philip Guo, Michael Terry, and the Center for Advanced Study in the Behavioral Sciences (CASBS) community for their insights, discussions, and support. Lastly, all locations featured in Smallville are inspired by real-world locations that Joon has frequented as an undergraduate and graduate student---he thanks everyone there for feeding and supporting him all these years.


