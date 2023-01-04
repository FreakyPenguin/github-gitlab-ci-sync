FROM debian:latest
RUN apt-get update \
 && apt-get install -y \
	git \
	npm \
	python-is-python3 \
	python3-aiohttp \
	python3-pip \
	wget \
	nano \
	vim \
 && rm -rf /var/lib/apt/lists/* \
 && npm install --global smee-client \
 && pip install pyyaml
COPY . /app
WORKDIR /app
CMD /bin/bash /app/run.sh
