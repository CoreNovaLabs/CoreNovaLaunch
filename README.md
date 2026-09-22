# CoreNovaLaunch

English | [简体中文](README.zh-CN.md)

The application verification and publishing component of **CoreNova Launch**. It registers self-hosted applications, tests digest-pinned container images, and publishes the verified data consumed by the website.

```text
App registration → Version & image resolution → Container verification → Publish Gate → Website data
                                                     ↑
                                              Platform Contract
```

- **Application Verification** runs the container stage: Docker Compose, readiness checks, version assertions, predefined tests, and Playwright screenshots. This stage creates no AWS resources.
- **Production Check** gates release for apps that declare `deployment.production_contract`: the container stage only stages an isolated candidate, then `production-verify` creates a one-off real stack, probes it over SSM, and promotes by CAS only after every check and the cleanup confirmation pass. It creates billable resources. Apps without the declaration keep the legacy two-phase publish path.
- **Publish Gate** commits manifests, reports, screenshots, and indexes through a two-phase process; the website consumes published data instead of inferring verification results. Promotion never clears a manually registered `deploy.hold`.
- **Golden Verification** separately tests the AWS platform and produces a Platform Contract. It creates billable resources.

This repository does not build AMIs or implement the website. The production publishing path remains single-container v1; experimental stack v2 does not replace existing deployments.

[Quick start](#quick-start) · [Configuration](#configuration-and-artifacts) · [Add an app](#add-an-application) · [Deployment safety](#deployment-safety) · [Maintenance](#maintenance) · [Reference](#reference)

## Quick start

Run all commands from the `CoreNovaLaunch` directory.

### 1. Install dependencies

Use Python **3.10+** (3.12 recommended). Application verification also requires a running Docker daemon with Compose v2, network access to GitHub and the image registry, and Playwright Chromium. The examples use an authenticated GitHub CLI (`gh auth login`) to supply `GITHUB_TOKEN`.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

### 2. Run offline checks

After dependency installation, these checks require no Docker, AWS credentials, or network access:

```bash
.venv/bin/python scripts/verify/validate_app_schema.py --all
.venv/bin/python scripts/verify/golden_verify.py --check
.venv/bin/python -m pytest tests -q
```

### 3. Verify an application without publishing

Replace `ghost` with a name from [apps/](apps/).

```bash
GITHUB_TOKEN=$(gh auth token) .venv/bin/python \
  scripts/verify/run_application_verify.py --app ghost --no-publish --skip-ami-drift
```

- `--no-publish` stops before publication: it does not update `current.json` or the published indexes.
- `--skip-ami-drift` skips the AWS public-AMI drift lookup for local testing without AWS credentials. It does **not** bypass the other Platform Contract checks or establish new platform verification evidence.
- A valid, matching Platform Contract must already exist in the selected backend. A fresh checkout or changed platform assets may require a maintainer to provide or regenerate it; offline checks alone do not create one.

For normal verification with drift checking, omit `--skip-ami-drift` and provide the required AWS read credentials. Publishing is a separate, deliberate step under [Maintenance](#maintenance).

## Configuration and artifacts

[config/verify.yaml](config/verify.yaml) controls verification and publishing; [config/platform.yaml](config/platform.yaml) defines platform identity. Environment variables override configuration values.

| Setting | Purpose |
| --- | --- |
| `GITHUB_TOKEN` | Authenticate upstream release and repository queries. |
| `VERIFIED_BACKEND` | Select `dir` or `r2`; the checked-in default is `dir`. There is no automatic fallback between backends. |
| `VERIFIED_OUTPUT_DIR` | Local output directory; defaults to `data/`. |
| `CORENOVA_REGISTRY_MIRROR` | Optional registry prefix. Changes the pull path, not the image identity recorded in the Manifest. |
| `CORENOVA_PROBE_HOST` | Host reachable by the verifier when Docker runs elsewhere; for example, `host.docker.internal` where supported. |
| `R2_*` | R2 endpoint, bucket, public URL, and credentials when using the R2 backend. |
| `SITE_REPO` / `REPO_A_PAT` | Target repository and credential for the website rebuild notification. |

Do not put credentials in app registrations or commit them to Git. See [corenova/config.py](corenova/config.py) for configuration overrides and [the workflows](.github/workflows/) for CI environment wiring.

Local artifacts are kept under `data/`: `runs/{verification_id}/state.json`, reports, screenshots, and—when publishing to the `dir` backend—`verified/` records. With R2 selected, local artifacts are not an alternative website data source. `data/` is not tracked by Git.

## Add an application

Start with the [App Schema](contracts/app-schema.md) and [application profiles](contracts/app-profiles.md).

1. Generate a scaffold with `scripts/dev/new_app.py` (`--help` lists the required arguments). The generator leaves TODOs for facts that must be checked manually.
2. Complete `apps/{name}.yaml`: health endpoint, version assertion, persistent data path, and English/Chinese copy. Image templates must resolve to exact version tags, never moving tags such as `latest`.
3. Complete `apps/{name}/docker-compose.yml`. Inject the image, ports, public URL, and data directory through `CORENOVA_APP_IMAGE`, `CORENOVA_HOST_PORT`, `CORENOVA_CONTAINER_PORT`, `CORENOVA_APP_URL`, and `CORENOVA_DATA_DIR`.
4. Add pytest and Playwright coverage under `apps/{name}/tests/`. Assert observed behavior only; document unverified capabilities. Scenario slugs must be ASCII and match `website.screenshots_order`.
5. Validate the schema and run application verification with `--no-publish` first. Publish only after the gates pass. Add `{name}.svg` to the website repository's `public/icons/` separately.

Keep Compose behavior, deployment parameters, and capability descriptions consistent. Do not describe a host mount, exposed port, or authentication mechanism as available unless the deployment actually provides it.

## Deployment safety

These settings apply to the current [single-container CloudFormation template](templates/cloudformation/fixed/app.yaml), not automatically to existing instances.

| Capability | Default and opt-in behavior |
| --- | --- |
| Web access | `AllowedWebCidr=127.0.0.1/32`: HTTP, HTTPS, and health paths allow local access only. Use SSM forwarding for initial setup. |
| Host Docker control | `DockerSocketAccess=false`. Enabling it grants host-root-equivalent control; restrict access and configure authentication first. |
| Extra business ports | `ExtraTcpPort=0` and `ExtraUdpPort=0` disable publication. When enabled, Docker mappings and security-group rules use the selected ports and `ExtraPortIngressCidr`. |
| Uploads and WebSocket | Shared HTTP/HTTPS configuration; `MaxUploadSizeMb=100` MiB, adjustable up to 10240 MiB. Application limits still apply. Streaming and WebSocket upgrades are enabled; proxy read/write timeouts are 3600 seconds. |
| Persistent data | For non-root image users, empty directories receive image-user ownership; existing nonempty directories with mismatched ownership cause startup to fail. No recursive ownership or permission changes are made. |

For direct Web access, allow only an administrator/VPN CIDR and narrow `HttpIngressCidr` accordingly. A CIDR allowlist is not application authentication. Configure authentication and HTTPS before intentionally exposing a public service.

<details>
<summary>SSM access and application-specific notes</summary>

With AWS CLI, the Session Manager plugin, and appropriate IAM permissions, replace the instance ID and start a tunnel:

```bash
aws ssm start-session --target i-xxxxxxxxxxxxxxxxx \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["80"],"localPortNumber":["8080"]}'
```

Open `http://localhost:8080`. For an app used exclusively through this tunnel, set `LaunchUrl` to the same address.

- **code-server / data ownership:** numeric image UIDs do not require an in-image shell or `id`; a fresh ext4 volume's `lost+found` is allowed. Environment files use the `corenova-app` group.
- **URL injection:** `AppUrlEnvironmentName` takes precedence. `ExtraEnvironment` expands only `${CORENOVA_APP_URL}`; a required URL that cannot be resolved or an unknown placeholder causes failure. Configuration is not evaluated as shell code.
- **Portainer:** the default verifies service startup, not management of the host Docker engine. Local management requires explicit socket access; remote environments require separate setup.
- **Netdata:** container-agent scope only; no host `/proc`, `/sys`, cgroup, Docker socket, or extra capabilities. Full host monitoring needs separate assessment. Netdata Cloud claiming does not protect the local Agent endpoint.
- **Syncthing:** direct sync requires `ExtraTcpPort=22000`, `ExtraUdpPort=22000`, and a peer CIDR in `ExtraPortIngressCidr`. The GUI remains loopback-bound; UDP 21027 LAN discovery is not exposed. Rules target the supplied `SecurityGroupId`, which should be instance-specific when using an existing network.
- **Vikunja:** new deployments keep SQLite and attachments in `/db` via `VIKUNJA_FILES_BASEPATH=/db`. Old containers may still hold attachments in `/app/vikunja/files`; export and restore-test them before replacing the container.

</details>

**Existing deployments require a maintenance plan.** Updating a template or CloudFormation parameters does not automatically replay cfn-init. Back up data, verify recovery, then explicitly apply the configuration. A database-engine change needs a separate backup, migration, restore, cutover, and rollback design—not just a new template and a restart. Experimental v2 does not pass through the v1 publishing gate.

## Maintenance

<details>
<summary>Resolve versions and images without running verification</summary>

These commands query GitHub and the image registry; they do not verify or publish an application.

```bash
GITHUB_TOKEN=$(gh auth token) .venv/bin/python scripts/verify/resolve_version.py --app ghost
GITHUB_TOKEN=$(gh auth token) .venv/bin/python scripts/verify/resolve_image.py --app ghost
```

</details>

<details>
<summary>Change templates and run local regression tests</summary>

Edit `templates/cloudformation/fixed/init/*.sh`, then synchronize the embedded scripts in `app.yaml` and regenerate `canary.yaml`:

```bash
.venv/bin/python scripts/verify/golden_verify.py --sync-init
.venv/bin/python scripts/verify/golden_verify.py --check
.venv/bin/python -m ruff check corenova scripts tests apps
.venv/bin/python -m pytest tests -q
```

Optional Docker integration tests use preloaded local images and clean up their temporary containers and volumes. Missing images fail the tests; they are not pulled automatically.

```bash
CORENOVA_TEST_DOCKER=1 .venv/bin/python -m pytest tests/test_user_template.py -q
```

Local tests are not a substitute for AWS Golden Verification.

</details>

<details>
<summary>Run AWS Golden Verification — creates billable resources</summary>

Preview the plan without AWS calls:

```bash
.venv/bin/python scripts/verify/golden_verify.py --dry-run
```

Only with AWS credentials and authorization to create resources, run the actual verification:

```bash
.venv/bin/python scripts/verify/golden_verify.py
```

This creates a canary, runs platform probes, writes the Platform Contract, and attempts cleanup. Confirm cleanup has completed. Public-AMI mode installs Docker/Nginx with cfn-init and requires platform re-verification within 30 days.

</details>

<details>
<summary>Publish verified data — writes to the selected backend</summary>

Check the backend and credentials before running. Without `--no-publish`, a successful verification proceeds to publication and may notify the website repository when configured:

```bash
GITHUB_TOKEN=$(gh auth token) .venv/bin/python \
  scripts/verify/run_application_verify.py --app ghost
```

The Publish Gate requires all nine checks before committing `current.json`. R2 website data and the public S3 one-click template are separate publishing channels; template distribution uses `scripts/verify/build_user_template.py` and the `publish-template` workflow.

</details>

### CI workflows

| Workflow | Responsibility |
| --- | --- |
| `pr-checks` | Lint, app-schema validation, and repository tests. |
| `monitor-versions` | Discover upstream versions every six hours. |
| `application-verify` | Run the container stage for an application with app-level concurrency control; publish directly, or stage a candidate when `deployment.production_contract` is declared. |
| `production-verify` | Check an exact candidate on a real one-off stack and, only after cleanup is confirmed, promote it by CAS. Shares the `verify-<app>` concurrency group; creates billable resources. |
| `golden-verify` | Verify the AWS platform on dispatch or on the 1st and 16th of each month. |
| `publish-template` | Publish the one-click template and check anonymous readability. |
| `publish-site` | Notify the website repository to rebuild. |
| `reverify-failed` | Retry failures classified as `TRANSIENT`. |
| `sync-holds` | Project `deployment.hold` from `apps/*.yaml` into published `current.json` without touching verification data. |

## Reference

| Path | Contents |
| --- | --- |
| [apps/](apps/) | App registrations, Compose definitions, and application tests. |
| [corenova/](corenova/) | Verification, manifest generation, and publishing logic. |
| [scripts/](scripts/) | Verification, monitoring, and development entry points. |
| [templates/](templates/) | CloudFormation templates and initialization assets. |
| [tests/](tests/) | Repository regression tests. |

Contracts: [App Schema](contracts/app-schema.md) · [Platform Contract](contracts/platform-contract.md) · [Verification Manifest](contracts/verification-manifest.md) · [Deployment Contract](contracts/deployment-contract.md) · [Workflow State Machine](contracts/workflow-state-machine.md).

Application planning: [App roadmap](docs/app-roadmap-120.md).

In the umbrella workspace, architecture and cross-repository setup are documented under `../docs/`, with CI secret requirements in `../docs/repo-structure.md` §6. Its `docs/contracts/` directory is authoritative; this repository's `contracts/` contains mirrored copies. Keep the English and Chinese READMEs aligned when changing commands or operational boundaries.
