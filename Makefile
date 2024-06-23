help:
	@echo 'Usage: make [target]'
	@echo
	@echo 'Development Targets:'
	@echo '  venv      Create virtual Python environment for development.'
	@echo '  checks    Run linters and tests.'
	@echo
	@echo 'Deployment Targets:'
	@echo '  service   Remove, install, configure, and run app.'
	@echo '  rm        Remove app.'
	@echo '  help      Show this help message.'


# Development Targets
# -------------------

rmvenv:
	rm -rf ~/.venv/tzero venv

venv: FORCE
	python3 -m venv ~/.venv/tzero
	echo . ~/.venv/tzero/bin/activate > venv
	. ./venv && pip3 install pylint pycodestyle pydocstyle pyflakes isort

lint:
	. ./venv && ! isort --quiet --diff . | grep .
	. ./venv && pycodestyle .
	. ./venv && pyflakes .
	. ./venv && pylint -d C0115,C0116,R0903,R0911,R0913,R0914,W0718 tzero

test:
	python3 -m unittest -v

coverage:
	. ./venv && coverage run --branch -m unittest -v
	. ./venv && coverage report --show-missing
	. ./venv && coverage html

check-password:
	! grep -r '"password":' . | grep -vE '^\./[^/]*.json|Makefile|\.\.\.'

checks: lint test check-password

clean:
	rm -rf *.pyc __pycache__
	rm -rf .coverage htmlcov
	rm -rf dist tzero.egg-info


# Deployment Targets
# ------------------

service: rmservice
	adduser --system --group --home / tzero
	mkdir -p /opt/data/tzero/
	chown -R tzero:tzero . /opt/data/tzero/
	chmod 600 tzero.json
	systemctl enable "$$PWD/etc/tzero.service"
	systemctl daemon-reload
	systemctl start tzero
	@echo Done; echo

rmservice:
	-systemctl stop tzero
	-systemctl disable tzero
	systemctl daemon-reload
	-deluser tzero
	@echo Done; echo

pull-backup:
	mkdir -p ~/bkp/
	ssh splnx.net "tar -czf - -C /opt/data/ tzero/" > ~/bkp/tzero-$$(date "+%Y-%m-%d_%H-%M-%S").tgz
	ls -lh ~/bkp/

FORCE:
