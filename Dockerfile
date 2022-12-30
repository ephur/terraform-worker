FROM python:3.10.9-slim-bullseye


COPY . /usr/src/tfworker
WORKDIR /usr/src/tfworker

# apt is run with 2>/dev/null to squelch apt CLI warnings and make builds look cleaner, remove to debug
RUN apt update 2>/dev/null && \
    apt install -y --no-install-recommends \
        wget \
        unzip && 2>/dev/null \
    wget --quiet --output-document terraform.zip https://releases.hashicorp.com/terraform/0.13.7/terraform_0.13.7_linux_amd64.zip && \
    unzip terraform.zip && \
    rm terraform.zip && \
    mv terraform /usr/local/bin && \
    chmod 755 /usr/local/bin/terraform && \
    pip install . && \
    apt remove -y wget unzip 2>/dev/null && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /
ENTRYPOINT [ "/bin/bash" ]
