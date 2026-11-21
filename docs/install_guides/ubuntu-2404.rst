.. _install-ubuntu-2404:
.. os-image-location::

    [ubuntu-2404]
    download_type = 'html'
    url = 'https://cloud-images.ubuntu.com/noble/current/'
    url_xpath = '''
    concat(
        "../",
        substring-before(substring-after(/html/head/title, "daily ["), "]"),
        "/noble-server-cloudimg-amd64.img"
    )
    '''
    checksum_file_url_xpath = '''
    concat(
        "../",
        substring-before(substring-after(/html/head/title, "daily ["), "]"),
        "/SHA256SUMS"
    )
    '''

    [ubuntu-2404-arm]
    download_type = 'html'
    arch = 'aarch64'
    url = 'https://cloud-images.ubuntu.com/noble/current/'
    url_xpath = '''
    concat(
        "../",
        substring-before(substring-after(/html/head/title, "daily ["), "]"),
        "/noble-server-cloudimg-arm64.img"
    )
    '''
    checksum_file_url_xpath = '''
    concat(
        "../",
        substring-before(substring-after(/html/head/title, "daily ["), "]"),
        "/SHA256SUMS"
    )
    '''

    [ubuntu-2404-arm-raspi]
    download_type = 'html'
    arch = 'aarch64'
    machine_type = 'raspi3b'
    image_format = 'raw+xz'
    url = 'https://cdimage.ubuntu.com/ubuntu-server/noble/daily-preinstalled/current/'
    url_xpath = '''
    concat(
        /html/head/link[@rel="canonical"]/@href, "/noble-preinstalled-server-arm64+raspi.img.xz"
    )
    '''
    checksum_file_url_xpath = '"SHA256SUMS"'

==================================
Installing Red on Ubuntu 24.04 LTS
==================================

.. include:: _includes/supported-arch-x64+aarch64.rst

.. include:: _includes/linux-preamble.rst

-------------------------------
Installing the pre-requirements
-------------------------------

The fork supports Python 3.15. On Ubuntu 24.04, the ``deadsnakes`` PPA provides the interpreter and its development packages.

.. prompt:: bash

    sudo apt update
    sudo apt -y install software-properties-common
    sudo add-apt-repository -y ppa:deadsnakes/ppa

Now install the pre-requirements with apt:

.. prompt:: bash

    sudo apt -y install python3.15 python3.15-dev python3.15-venv git openjdk-25-jre-headless build-essential nano

.. Include common instructions:

.. include:: _includes/_create-env-with-venv-intro.rst

.. prompt:: bash

    python3.15 -m venv ~/redenv

.. include:: _includes/_create-env-with-venv-outro.rst

.. include:: _includes/install-and-setup-red-unix.rst
