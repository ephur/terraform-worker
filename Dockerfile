FROM python:3.9-slim-buster

COPY . /usr/src/tfworker
WORKDIR /usr/src/tfworker

# apt is run with 2>/dev/null to squelch apt CLI warnings and make builds look cleaner, remove to debug
RUN apt update 2>/dev/null && \
    apt install -y --no-install-recommends \
        git \
        wget \
        awscli \
        jq \
        tree \
        unzip && 2>/dev/null \
    wget --quiet --output-document terraform.zip https://releases.hashicorp.com/terraform/0.12.29/terraform_0.12.29_linux_amd64.zip && \
    unzip terraform.zip && \
    rm terraform.zip && \
    mv terraform /usr/local/bin && \
    chmod 755 /usr/local/bin/terraform && \
    wget --quiet --output-document sam.zip https://github.com/aws/aws-sam-cli/releases/latest/download/aws-sam-cli-linux-x86_64.zip && \
    unzip sam.zip -d sam-install && \
    ./sam-install/install && \
    rm sam.zip && \
    pip install tomlkit && \
    python ./setup.py install && \
    apt remove -y wget unzip 2>/dev/null && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /
ENTRYPOINT [ "/bin/bash" ]
