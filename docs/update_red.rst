============
Updating Red
============

Updating the MoldTroll fork
***************************

This fork tracks the `V3/develop branch on GitHub
<https://github.com/MoldTroll/Red-DiscordBot/tree/V3/develop>`_.
Git must be available on the host. Activate the virtual environment used by
your bot and stop every instance sharing that environment before updating.

For an existing installation of the fork, run the following as the bot's
service account:

.. code-block:: console

    $ redbot-update

The updater checks the fork branch, preserves installed extras, and installs
the exact commit it checked. After it finishes, restart the instances.

To migrate from an upstream installation, install the fork into a fresh virtual
environment using a Python version supported by the fork:

.. code-block:: console

    $ python -m pip install "Red-DiscordBot @ git+https://github.com/MoldTroll/Red-DiscordBot.git@V3/develop"
    $ python -m pip check

For PostgreSQL-backed instances, use ``Red-DiscordBot[postgres]`` in the
requirement above. Point your service at the new environment and start it
under its existing instance name so it uses the same bot data.

Staged updates for Linux music bot services
------------------------------------------

For one or more systemd-managed music instances with JSON storage and a shared
Lavalink node, the fork includes a staged updater. It installs the
exact head of the fork's branch into a fresh virtual environment, then updates
the main instance first and the others in their declared order. Each bot must
reconnect to Lavalink and pass its health checks before the next bot is stopped.

Install the helpers from a reviewed checkout using a root shell:

.. code-block:: console

    # install -d -m 0755 /usr/local/libexec/redbot-fork /usr/local/libexec/red-lavalink
    # install -m 0644 tools/redbot/update.py /usr/local/libexec/redbot-fork/
    # install -m 0644 tools/lavalink/{release,probe}.py /usr/local/libexec/red-lavalink/
    # install -m 0644 tools/redbot/redbot-fork-update.service /etc/systemd/system/

The host needs systemd, Git, iproute2, util-linux and system Python 3.11 or
newer with PyYAML. The bot account needs a supported Python with the
``venv`` module. Set the root-owned file ``/etc/default/redbot-fork-update``
for your installation. This example deploys ``main`` first:

.. code-block:: ini

    RED_INSTANCES="main secondary"
    RED_USER=red
    RED_ROOT=/home/red/.local/share/redbot-fork
    RED_PYTHON=/opt/python3.15/bin/python3.15
    RED_DATA_MAIN=/home/red/.local/share/Red-DiscordBot/data/main
    RED_DATA_SECONDARY=/home/red/.local/share/Red-DiscordBot/data/secondary
    LAVALINK_USER=lavalink
    LAVALINK_DIR=/srv/lavalink
    LAVALINK_PORT=2333
    HEALTH_TIMEOUT=120

Each ``RED_DATA_<UPPERCASE_INSTANCE>`` must be the existing absolute JSON
data directory containing ``core/settings.json`` and ``cogs/Audio/Audio.db``.
Use a separate data directory per instance. Under ``RED_ROOT/instances``,
create a symlink named for each instance pointing to its current, working
virtual environment. The bot account must own the instance links directory and
be able to create ``RED_ROOT/releases``. Configure ``red@.service`` to run as
``RED_USER`` with the following service settings:

.. code-block:: ini

    [Service]
    ExecStart=
    ExecStart=/home/red/.local/share/redbot-fork/instances/%i/bin/python -O -m redbot %I --no-prompt
    RuntimeDirectory=redbot-probe-%i
    RuntimeDirectoryMode=0700
    Environment=RED_AUDIO_PROBE_SOCKET=/run/redbot-probe-%i/check.sock

Preserve any existing instance-specific startup flags. Reload systemd, then
start each bot with the new settings during an idle period. For subsequent
fork updates, run:

.. code-block:: console

    # systemctl start redbot-fork-update.service
    # journalctl -u redbot-fork-update.service -n 50 --no-pager

Before each service stop, the updater checks each bot's voice connections
across all Discord servers. If either bot is in a voice channel, even with
playback paused or stopped, the update waits. The updater also checks for
active Lavalink playback and shares a lock with the Lavalink updater.

If only listeners are in the designated test channel, the rollout proceeds.
The bot skips the playback trial in that channel and the updater logs that
voice playback was not verified. It still checks the running virtual
environment, installed commit, bot connection, Lavalink API and configured
YouTube plugin. When the channel is empty, the bot can run the playback trial.

When upgrading from an older release, install the updated
``tools/redbot/update.py`` and ``tools/lavalink/probe.py`` helpers together.
A running bot with the older probe still uses its original idle check, so the
test channel may need to be empty for that first rollout.

To check the fork automatically, install and enable the systemd timer:

.. code-block:: console

    # install -m 0644 tools/redbot/redbot-fork-autoupdate.{service,timer} /etc/systemd/system/
    # systemctl daemon-reload
    # systemctl enable --now redbot-fork-autoupdate.timer
    # systemctl list-timers --all redbot-fork-autoupdate.timer
    # journalctl -u redbot-fork-autoupdate.service -n 50 --no-pager

The timer checks 15 seconds after activation and every five minutes thereafter.
It leaves running bots alone when they already use the fork's current commit.
By default the first name in ``RED_INSTANCES`` is the main instance. With
``RED_INSTANCES="mold moldsub"``, the updater verifies ``mold`` before switching
``moldsub``. To select a later entry as the main instance, set
``RED_MAIN_INSTANCE`` to its name. Change the configured order after any
in-progress rollout or rollback has finished so recovery uses the same order.
If a bot joins voice between the two, the timer resumes the rollout on an
idle check. After a candidate fails to start or pass health checks and the
previous runtimes are restored, automatic checks wait six hours before
retrying that commit. A newer commit is eligible on the next check. An
operator can retry immediately with ``redbot-fork-update.service``.

An instance that was cleanly stopped before the update stays stopped. A new
release waits for the main instance before proceeding to other instances.
When a stopped secondary starts again, the timer can update it without
restarting a main instance already running the target commit. A failed or
transitioning service must recover before a new rollout can start.

After a successful rollout, each updated instance sends its own opted-in
owners a DM with its name and the deployed commit. This also works with a
single instance. To have one bot send a consolidated DM, set the following
in ``/etc/default/redbot-fork-update``:

.. code-block:: ini

    RED_INSTANCES="mold moldsub"
    RED_UPDATE_DM_SENDER=mold

The update order and the DM sender are configured independently.
``mold`` sends the notice after verification. If the designated sender is
offline when a notice is due, that notice waits for it to run the deployed
commit. To allow ``moldsub`` to send on its behalf, set
``RED_UPDATE_DM_FALLBACK=moldsub``. The fallback setting accepts
a space-separated list of backup senders in priority order. All senders must
be in ``RED_INSTANCES``.

The timer retries unacknowledged notices for the latest deployed commit
through the selected bot's private Audio socket. A temporary Discord failure
keeps the same sender. Changes to the sender settings apply to new notices.
Starting a bot, checking an unchanged commit, or rolling back a failed update
does not create a notice.

The root-only ``/var/lib/redbot-fork-update`` directory holds the transaction
journal and per-instance snapshots. Before switching a bot, the updater stops
it and archives its JSON files and SQLite Audio database, including any WAL or
journal sidecars. A failed deployment restores the previous environment and
snapshot for each affected instance. This also reverts changes made to that
instance's saved data after its snapshot. After an interruption, start the
service again to recover, then again to retry the update. A failed rollback
keeps the transaction and backups for another recovery attempt.

After a successful rollout the updater keeps the active and preceding managed
environments and the two latest successful snapshot sets. After a failed
rollout it retains the latest failed snapshot set as well. Bootstrap
environments remain outside this retention policy. Protect the backup directory
like your bot data because JSON files may contain credentials.

Upstream releases
*****************

The version-specific instructions below apply to upstream PyPI installations.

Updating to the latest version of Red has several benefits:

- New features and improvements are added.
- Bugs are fixed.
- Your bot is safe from security vulnerabilities that have been found.

Here are some things to consider to help make your upgrade as smooth as possible.

.. note::

    If you're developing for Red, you should also look for "Breaking changes" sections in release notes for each minor (X.Y.0) version that's been released since you last updated Red. To make the migration process easier, we also provide an extensive `incompatible_changes/index` document explaining the breaking changes we've made in more detail.

.. important::

    Make sure to read the **Read before updating** sections of the changelogs for all releases that were published since you last updated your instance. They contain important information and if you don't read them, you might run into some issues later.

Updating differs depending on the version you currently have. Next sections will explain how to upgrade to latest version of Red (|version|) from the version that is in the header of the section.

.. contents:: Choose the version you're currently on from the list below:
    :local:
    :depth: 1


Red 3.5.25 or newer
*******************

.. note::

    If you installed any package into Red's virtual environment other than Red,
    please note that they will not be kept after the update.

Windows
-------

If you have Red 3.5.25 or newer, you can upgrade by following these steps:

#. Shut your bot down.
#. Activate your venv with the following command:

    .. prompt:: batch

        "%userprofile%\redenv\Scripts\activate.bat"
#. Update Red with this command:

    .. prompt:: batch
        :prompts: (redenv) C:\\>

        redbot-update
#. Start your bot.

Linux & Mac
-----------

If you have Red 3.5.25 or newer, you can upgrade by following these steps:

#. Shut your bot down.
#. Activate your virtual environment.
  
    If you used ``venv`` for your virtual environment, use:

    .. prompt:: bash

        source ~/redenv/bin/activate

#. Update Red with this command:

    .. prompt:: bash
        :prompts: (redenv) $

        redbot-update
#. Start your bot.

Red 3.5.0-3.5.24
****************

Windows
-------

If you have a Red version between 3.5.0 and 3.5.24, you can upgrade by following these steps:

#. Shut your bot down.
#. Activate your venv with the following command:

    .. prompt:: batch

        "%userprofile%\redenv\Scripts\activate.bat"
#. Update Red with this command:

    .. prompt:: batch
        :prompts: (redenv) C:\\>

        python -m pip install -U "Red-DiscordBot==3.5.25"

    .. attention::

        If you're using PostgreSQL data backend, replace ``Red-DiscordBot`` in the second command with ``Red-DiscordBot[postgres]``
#. Start your bot.
#. If you have any 3rd-party cogs installed, we highly recommend you update them with this command in Discord: ``[p]cog update`` (``[p]`` is considered as your prefix)

Linux & Mac
-----------

If you have a Red version between 3.5.0 and 3.5.24, you can upgrade by following these steps:

#. Shut your bot down.
#. Activate your virtual environment.
  
    If you used ``venv`` for your virtual environment, use:

    .. prompt:: bash

        source ~/redenv/bin/activate

#. Update Red with this command:

    .. prompt:: bash
        :prompts: (redenv) $

        python -m pip install -U 'Red-DiscordBot==3.5.25'

    .. attention::

        If you're using PostgreSQL data backend, replace ``Red-DiscordBot`` in the second command with ``Red-DiscordBot[postgres]``
#. Start your bot.
#. If you have any 3rd-party cogs installed, we highly recommend you update them with this command in Discord: ``[p]cog update`` (``[p]`` is considered as your prefix)

Red 3.2.0-3.4.19
****************

Windows
-------

If you have a Red version between 3.2.0 and 3.4.19, you can upgrade by following these steps:

#. Shut your bot down.
#. Activate your venv with the following command:

    .. prompt:: batch

        "%userprofile%\redenv\Scripts\activate.bat"
#. Update Red with this command:

    .. prompt:: batch
        :prompts: (redenv) C:\\>

        python -m pip install -U "Red-DiscordBot==3.5.25"

    .. attention::

        If you're using PostgreSQL data backend, replace ``Red-DiscordBot`` in the second command with ``Red-DiscordBot[postgres]``
#. If you're using **any** 3rd-party cogs:

    #. Start your bot with ``--no-cogs --load-cogs downloader`` flags, for example:

        .. prompt:: batch
            :prompts: (redenv) C:\\>

            redbot <your instance name> --no-cogs --load-cogs downloader

    #. Wait for the bot to start.
    #. Run the ``[p]cog update`` command on Discord.
    #. Wait for the bot to stop typing and send a response. This step may take a while.
    #. Shutdown the bot with the ``[p]shutdown`` command.
#. Start your bot as you normally do.

Linux & Mac
-----------

If you have a Red version between 3.2.0 and 3.4.19, you can upgrade by following these steps:

#. Shut your bot down.
#. Activate your virtual environment.
  
    If you used ``venv`` for your virtual environment, use:

    .. prompt:: bash

        source ~/redenv/bin/activate

#. Update Red with this command:

    .. prompt:: bash
        :prompts: (redenv) $

        python -m pip install -U 'Red-DiscordBot==3.5.25'

    .. attention::

        If you're using PostgreSQL data backend, replace ``Red-DiscordBot`` in the second command with ``Red-DiscordBot[postgres]``
#. **If you're using any 3rd-party cogs:**

    #. Start your bot with ``--no-cogs --load-cogs downloader`` flags, for example:

        .. prompt:: bash
            :prompts: (redenv) $

            redbot <your instance name> --no-cogs --load-cogs downloader

    #. Wait for the bot to start.
    #. Run the ``[p]cog update`` command on Discord.
    #. Wait for the bot to stop typing and send a response. This step may take a while.
    #. Shutdown the bot with the ``[p]shutdown`` command.
#. Start your bot as you normally do.

Red 3.1.X
*********

If you have Red 3.1.X, you will need to follow the install instructions for your operating system. Make sure that you turn your bot off first.

Go to the `install_guides/index` guide and follow every step to ensure you have all dependencies up-to-date and only skip the ``redbot-setup`` step as you already have a bot instance.

**If you already have Red installed in a virtual environment, you will need to delete it before starting this process.**

.. attention::

    Red 3.2 dropped support for the MongoDB driver

     - If you were not using the MongoDB driver, this does not affect you.
     - If you were using a 3rd party cog which required MongoDB, it probably still does.
     - If you were using the MongoDB driver, **prior to launching your instance after update**,
       you will need to run the following commands to convert:

         .. prompt:: bash
           :prompts: (redenv) $

           python -m pip install dnspython~=1.16.0 motor~=2.0.0 pymongo~=3.8.0
           redbot-setup convert [instancename] json


Red 3.0.2 and older
*******************

.. important::

    Red 3.2 dropped support for the MongoDB driver

     - If you were not using the MongoDB driver, this does not affect you.
     - If you were using a 3rd party cog which required MongoDB, it probably still does.
     - If you were using the MongoDB driver, **prior to updating**, you will need to convert your data to JSON backend,
       using following command:

         .. prompt:: bash
           :prompts: (redenv) $

           redbot-setup --edit

If you have Red 3.0.2 or older, you will need to follow the install instructions for your operating system. Make sure that you turn your bot off first.

Go to the `install_guides/index` guide and follow every step to ensure you have all dependencies up-to-date and only skip the ``redbot-setup`` step as you already have a bot instance.

**If you already have Red installed in a virtual environment, you will need to delete it before starting this process.**
