ARG BUILD_FROM
FROM $BUILD_FROM

RUN pip3 install aiohttp pymodbus --break-system-packages

COPY run.sh /run.sh
COPY bridge.py /bridge.py

RUN chmod +x /run.sh

CMD ["/run.sh"]
