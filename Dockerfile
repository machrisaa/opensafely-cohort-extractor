FROM ubuntu:bionic

ENV DEBIAN_FRONTEND noninteractive
ENV DEBCONF_NONINTERACTIVE_SEEN true
ENV UBUNTU_VERSION $ubuntuversion
RUN apt-get update
RUN apt-get -y upgrade

# Python dependencies
RUN apt-get install -y --no-install-recommends make build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev wget curl llvm libncurses5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev

# Install Python
RUN apt-get install -y python3.7 python3.7-dev git docker.io sqlite3
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3.7 1
RUN curl https://bootstrap.pypa.io/get-pip.py | python

# Install mssql
RUN apt-get install -y gnupg
RUN curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add -
RUN curl https://packages.microsoft.com/config/ubuntu/18.04/prod.list > /etc/apt/sources.list.d/mssql-release.list
RUN apt-get update
RUN ACCEPT_EULA=Y apt-get install -y msodbcsql17
RUN ACCEPT_EULA=Y apt-get install -y mssql-tools
ENV PATH=$PATH:/opt/mssql-tools/bin

RUN mkdir /workspace
RUN mkdir /app
WORKDIR /app

# Install pip and requirements
COPY requirements.txt /app
# Extra dependencies needed by python packages
RUN apt-get install -y unixodbc-dev
RUN pip install --requirement requirements.txt
RUN apt-get update
RUN apt-get install -y docker.io

COPY . /app

RUN python setup.py develop

WORKDIR /workspace

# It's helpful to see output immediately
ENV PYTHONUNBUFFERED=True

ENTRYPOINT ["cohortextractor"]
