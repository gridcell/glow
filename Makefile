# Container images for local development.
#
#   make images          build the toolpack images as local/<toolpack>:dev, plus
#                        glow-exec, engine and sandbox when their Dockerfiles exist
#   make images-test     run the wrapper tests inside the built images
#   make images-push REGISTRY=ghcr.io/sparkgeo
#                        tag and push the toolpack images to REGISTRY
#   make images-lock REGISTRY=ghcr.io/sparkgeo
#                        pin each manifest to its pushed digest and regenerate
#                        toolpacks/registry.lock.yaml

DOCKER ?= docker
REGISTRY ?=
# Images that other issues add; each is built only when its Dockerfile exists.
OPTIONAL_IMAGES := glow-exec engine sandbox

.PHONY: images images-test images-push images-lock

images:
	$(DOCKER) build -t local/gdal:dev toolpacks/gdal
	$(DOCKER) build -t local/stac:dev toolpacks/stac
	# prescient.render_from_color_table ships in the stac image for now.
	$(DOCKER) tag local/stac:dev local/prescient:dev
	@for name in $(OPTIONAL_IMAGES); do \
		if [ -f "$$name/Dockerfile" ]; then \
			echo "$(DOCKER) build -t local/$$name:dev $$name"; \
			$(DOCKER) build -t "local/$$name:dev" "$$name" || exit 1; \
		fi; \
	done

images-test:
	GLOW_IMAGE_TESTS=1 uv run pytest toolpacks/tests

images-push:
	@test -n "$(REGISTRY)" || { echo "set REGISTRY, for example REGISTRY=ghcr.io/sparkgeo"; exit 2; }
	$(DOCKER) tag local/gdal:dev $(REGISTRY)/glow-gdal:dev
	$(DOCKER) tag local/stac:dev $(REGISTRY)/glow-stac:dev
	$(DOCKER) push $(REGISTRY)/glow-gdal:dev
	$(DOCKER) push $(REGISTRY)/glow-stac:dev

images-lock:
	@test -n "$(REGISTRY)" || { echo "set REGISTRY, for example REGISTRY=ghcr.io/sparkgeo"; exit 2; }
	uv run python scripts/images_lock.py --registry $(REGISTRY) --docker $(DOCKER)
