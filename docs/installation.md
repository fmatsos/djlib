# Installer djlib

`djlib` est un simple CLI Python. Deux façons de l'installer :

- **Conteneur Proxmox LXC** — le mode de production visé par le projet :
  `/music` est monté physiquement en lecture seule et `/data` est le seul
  espace inscriptible (voir [`architecture.md`](architecture.md#le-principe-fondateur--ne-jamais-toucher-à-la-source)).
- **Installation manuelle sur n'importe quel poste** — pour développer,
  tester, ou simplement utiliser djlib sur une machine qui n'est pas un
  conteneur Proxmox (Linux, macOS, ou Windows via WSL).

Dans les deux cas, `djlib doctor` est la façon la plus rapide de vérifier
qu'une installation est saine (voir [`commandes.md`](commandes.md#djlib-doctor)).

## Prérequis communs

- Python ≥ 3.12
- Trois exécutables système, sur le `PATH` : `exiftool`, `ffprobe` (fourni
  par FFmpeg), `fpcalc` (fourni par Chromaprint)

---

## Option A — Conteneur Proxmox LXC (production)

### Automatisé, à distance : une seule commande, sans rien cloner

Comme les scripts communautaires Proxmox, tout se joue en une commande
lancée sur l'hôte Proxmox VE, en root, sans rien avoir cloné au préalable :

```bash
CTID=200 MUSIC_SRC=/mnt/tank/djing DATA_SRC=/mnt/tank/djlib \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/fmatsos/djlib/main/infra/lxc/create-container.sh)"
```

`create-container.sh` va chercher lui-même ses scripts compagnons
(`configure-mounts.sh`, `bootstrap.sh`, `install-djlib.sh`) sur GitHub
lorsqu'ils ne sont pas déjà à côté de lui — cette seule commande suffit
donc à tout installer.

### Automatisé, depuis un clone local

Si le dépôt est déjà cloné sur l'hôte, lancez le même script depuis son
emplacement — il utilisera alors les scripts compagnons présents sur le
disque, sans avoir besoin du réseau pour lui-même (le code de djlib est
tout de même récupéré par `install-djlib.sh`, à l'intérieur du conteneur) :

```bash
CTID=200 MUSIC_SRC=/mnt/tank/djing DATA_SRC=/mnt/tank/djlib \
  ./infra/lxc/create-container.sh
```

Dans les deux cas, ce script crée le conteneur LXC (ou réutilise `CTID`
s'il existe déjà), monte `/music` (lecture seule) et `/data`, met à jour
les paquets système, installe les prérequis, installe djlib dans un
environnement virtuel dédié, écrit la configuration par défaut dans
`/etc/djlib/config.toml`, migre la base de données, et termine par un
`djlib doctor`. Une fois terminé :

```bash
pct enter 200
djlib scan
```

`djlib` est directement disponible sur le `PATH` à l'intérieur du
conteneur. **Relancer ce même script sur un `CTID` existant** met à jour
les paquets et la dernière version de djlib — il ne recrée jamais le
conteneur depuis zéro.

### Manuel, étape par étape

Si vous préférez piloter chaque étape vous-même (ou que le conteneur existe
déjà, provisionné autrement) :

1. **Créer le conteneur** (`pct create ...`) à partir d'un template Debian.
   `pveam list <storage>` liste ce qui est déjà téléchargé ;
   `pveam available --section system` liste ce qui peut l'être.
2. **Monter les répertoires source et état :**
   ```bash
   ./infra/lxc/configure-mounts.sh <ctid>
   ```
   Éditez d'abord le script si vos chemins hôte diffèrent de
   `/mnt/tank/djing` / `/mnt/tank/djlib`.
3. **Installer les prérequis système et créer l'environnement virtuel**, à
   l'intérieur du conteneur :
   ```bash
   ./infra/lxc/bootstrap.sh
   ```
4. **Installer djlib**, écrire la configuration par défaut et migrer la
   base, à l'intérieur du conteneur :
   ```bash
   ./infra/lxc/install-djlib.sh
   ```
   Définissez `DJLIB_REPO_URL` / `DJLIB_REPO_REF` au préalable pour
   installer depuis un fork ou une branche/tag précis.
5. **Vérifier :**
   ```bash
   djlib doctor
   ```

### Mettre à jour un conteneur existant

- Relancez `infra/lxc/create-container.sh` sur le même `CTID` (paquets et
  djlib mis à jour, conteneur non recréé) ;
- ou, à l'intérieur du conteneur, `infra/lxc/install-djlib.sh` seul, pour ne
  mettre à jour que djlib sans toucher aux paquets système.

### Exécuter une commande longue en tâche de fond

`djlib rebuild` (ou un `djlib scan`/`djlib duplicates run` sur une très
grosse bibliothèque) peut prendre plusieurs minutes. Pour la laisser
tourner sans garder de session ouverte — par exemple pendant une absence —
la façon la plus fiable est de créer une unité `systemd` transitoire,
gérée par le PID 1 du conteneur plutôt que par la session `pct
exec`/`pct enter` :

```bash
pct exec <CTID> -- systemd-run --unit=djlib-rebuild --collect \
  --property=StandardOutput=append:/var/log/djlib-rebuild.log \
  --property=StandardError=append:/var/log/djlib-rebuild.log \
  /opt/djlib-venv/bin/djlib rebuild
```

Puis, pour suivre ou vérifier l'exécution :

```bash
pct exec <CTID> -- systemctl status djlib-rebuild    # état courant
pct exec <CTID> -- journalctl -u djlib-rebuild -f     # logs en direct
```

> **Pourquoi pas un simple `nohup ... &` ?** `pct exec` s'appuie sur
> `lxc-attach` : le process en arrière-plan reste rattaché à la même
> session/cgroup que la commande attachée, qui est souvent nettoyée dès que
> `pct exec` se termine — même avec `nohup`, qui ne protège que du signal
> `SIGHUP`, pas d'un nettoyage de cgroup. `systemd-run` évite entièrement ce
> problème : le process ne dépend plus de la session d'attachement.

---

## Option B — Installation manuelle, sur n'importe quel poste

Utile pour développer, tester, ou faire tourner djlib ailleurs que dans un
conteneur Proxmox.

### 1. Installer les prérequis système

**Debian / Ubuntu :**

```bash
sudo apt-get install exiftool ffmpeg libchromaprint-tools
```

**macOS (Homebrew) :**

```bash
brew install exiftool ffmpeg chromaprint
```

**Windows :** utilisez [WSL](https://learn.microsoft.com/windows/wsl/) avec
une distribution Debian/Ubuntu, puis suivez les instructions Debian/Ubuntu
ci-dessus — djlib n'est pas testé nativement hors Linux/macOS.

### 2. Installer djlib

```bash
python -m pip install -e '.[dev]'
djlib --help
```

### 3. Configurer

Copiez `config.example.toml`, ajustez `music_root` / `data_root`, puis
pointez djlib dessus via la variable d'environnement `DJLIB_CONFIG` :

```bash
cp config.example.toml /etc/djlib/config.toml
# éditez music_root / data_root dans /etc/djlib/config.toml
DJLIB_CONFIG=/etc/djlib/config.toml djlib doctor
```

Sans `DJLIB_CONFIG`, djlib utilise par défaut `music_root=/music` et
`data_root=/data`.

### Mettre à jour une installation locale

```bash
python -m pip install -e '.[dev]' --upgrade
```

---

## Variables d'environnement

### Exécution (`djlib`, en CLI)

| Variable | Rôle | Valeur par défaut |
| --- | --- | --- |
| `DJLIB_CONFIG` | Chemin vers un fichier TOML de configuration (voir `config.example.toml`) fournissant `music_root`, `data_root` et les seuils `[duplicates]`. | non défini → `music_root=/music`, `data_root=/data` |
| `DJLIB_REPO_ROOT` | Emplacement du dépôt contenant `alembic.ini`/`alembic/`, utilisé par le contrôle de migration de `djlib doctor` et par `djlib rebuild` pour exécuter/inspecter les migrations. Utile seulement pour une installation non éditable (ex. le conteneur LXC, où `install-djlib.sh` la positionne automatiquement) — une installation de développement (`pip install -e`) se débrouille seule. | non défini → déduit de l'emplacement du fichier source (ne fonctionne que pour une installation éditable) |

### `infra/lxc/create-container.sh` (provisionne le conteneur LXC)

| Variable | Rôle | Valeur par défaut |
| --- | --- | --- |
| `CTID` | Identifiant du conteneur LXC à créer ou réutiliser. | *(obligatoire)* |
| `HOSTNAME` | Nom d'hôte du conteneur. | `djlib` |
| `STORAGE` | Stockage Proxmox pour le disque racine du conteneur. | `local-lvm` |
| `TEMPLATE_STORAGE` | Stockage contenant le template du système. | `local` |
| `TEMPLATE` | Template du système : un volid complet (`local:vztmpl/debian-13-standard_...`) ou juste son nom de fichier/basename dans `TEMPLATE_STORAGE`, avec ou sans l'extension d'archive. | la version la plus récente de `debian-<N>-standard` déjà téléchargée (à défaut, disponible) pour l'architecture de l'hôte |
| `ARCH` | Architecture du conteneur à faire correspondre lors de la sélection automatique du template. | architecture de l'hôte (`dpkg --print-architecture`) |
| `ROOTFS_SIZE_GB` | Taille du disque racine, en Go. | `8` |
| `MEMORY_MB` | RAM, en Mo. | `2048` |
| `SWAP_MB` | Swap, en Mo. | `512` |
| `CORES` | Nombre de cœurs CPU. | `2` |
| `BRIDGE` | Pont réseau. | `vmbr0` |
| `NET_CONFIG` | Chaîne `pct --net0` complète (remplace `BRIDGE`). | `name=eth0,bridge=$BRIDGE,ip=dhcp` |
| `MUSIC_SRC` | Chemin hôte de l'archive DJ source, monté en lecture seule sur `/music`. | `/mnt/tank/djing` |
| `DATA_SRC` | Chemin hôte pour l'état de djlib, monté en lecture/écriture sur `/data`. | `/mnt/tank/djlib` |
| `DJLIB_REPO_URL` | Dépôt Git depuis lequel installer djlib, à l'intérieur du conteneur. | `https://github.com/fmatsos/djlib.git` |
| `DJLIB_REPO_REF` | Référence Git (branche/tag) à installer, et à récupérer pour les scripts compagnons de ce script lorsqu'il est lancé à distance. | `main` |
| `DJLIB_RAW_BASE` | URL de base pour récupérer les scripts compagnons manquants, lorsque lancé sans clone local. | `https://raw.githubusercontent.com/fmatsos/djlib` |

### `infra/lxc/install-djlib.sh` (installe/met à jour djlib dans le conteneur)

| Variable | Rôle | Valeur par défaut |
| --- | --- | --- |
| `DJLIB_REPO_URL` | Dépôt Git depuis lequel cloner/récupérer djlib. | `https://github.com/fmatsos/djlib.git` |
| `DJLIB_REPO_REF` | Référence Git (branche/tag) à installer. | `main` |
| `DJLIB_SRC_DIR` | Emplacement du code source de djlib à l'intérieur du conteneur. | `/opt/djlib` |
| `DJLIB_VENV` | Environnement virtuel Python (créé par `bootstrap.sh`) dans lequel installer djlib. | `/opt/djlib-venv` |
| `DJLIB_CONFIG_DIR` | Répertoire pour la configuration par défaut (`config.toml`). | `/etc/djlib` |
| `DJLIB_DATA_ROOT` | Racine de la structure `/data` (cache/curation/rapports/décisions/logs). | `/data` |

---

## Dépannage rapide

`djlib doctor` reste le premier réflexe : chaque ligne `[FAIL]` explique la
cause et, souvent, la commande exacte à lancer pour corriger (voir
[`commandes.md`](commandes.md#djlib-doctor)).

Un piège classique dans un conteneur LXC : `pct enter` ouvre un shell
**non-login** (via `lxc-attach`), qui ne source jamais `/etc/profile` et
peut démarrer avec un `PATH` minimal. `install-djlib.sh` s'en protège déjà
(lien symbolique dans `/usr/bin` en plus de `/usr/local/bin`, variables
écrites aussi dans `/etc/bash.bashrc`), mais si `djlib` reste introuvable
après une installation manuelle personnalisée, c'est la première piste à
vérifier.
