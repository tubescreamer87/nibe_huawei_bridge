ARG BUILD_FROM
FROM $BUILD_FROM

RUN pip3 install aiohttp "pymodbus>=3.0,<3.7" --break-system-packages

# BUILD_VERSION changes every release — forces cache invalidation for COPY layers
ARG BUILD_VERSION
RUN echo "$BUILD_VERSION" > /addon_version
# Cache buster: 2.6.5
ARG CACHE_BUST=2.6.0

COPY run.sh /run.sh
COPY bridge.py /bridge.py

RUN chmod +x /run.sh

CMD ["/run.sh"]
