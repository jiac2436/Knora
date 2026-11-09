ARG CRAWL4AI_BASE_IMAGE=unclecode/crawl4ai:0.9.4
FROM ${CRAWL4AI_BASE_IMAGE}

USER root
COPY apply_patch.py regression_test.py proxy_regression_test.py local_dns.py start.py /opt/pka-crawl4ai/
RUN python /opt/pka-crawl4ai/apply_patch.py \
    && python /opt/pka-crawl4ai/regression_test.py \
    && python /opt/pka-crawl4ai/proxy_regression_test.py
ENV HOME=/home/appuser
ENTRYPOINT ["python", "/opt/pka-crawl4ai/start.py"]
CMD ["bash", "entrypoint.sh"]
