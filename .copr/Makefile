.PHONY: srpm

srpm:
	dnf -y install ca-certificates git-core python3 rpm-build
	python3 build-source-rpm.py --outdir "$(outdir)" --spec "$(spec)"
