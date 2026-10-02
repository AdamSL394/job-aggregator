# Lambda container image (Option B). Build for linux/amd64 even on an
# ARM laptop -- Lambda's default architecture is x86_64:
#   docker build --platform linux/amd64 -t job-aggregator .
FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt ${LAMBDA_TASK_ROOT}
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}"

# job_aggregator/ includes profiles_local.py (real resume text + Sheet IDs) --
# gitignored for git, but present on disk and deliberately baked into this
# image, which only ever goes to your own private ECR repo, never to GitHub.
COPY job_aggregator/ ${LAMBDA_TASK_ROOT}/job_aggregator/
COPY config/ ${LAMBDA_TASK_ROOT}/config/
COPY data/ ${LAMBDA_TASK_ROOT}/data/

CMD ["job_aggregator.lambda_handler.handler"]
