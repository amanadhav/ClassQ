# ClassQ — Deployment Guide (AWS)

This guide walks through deploying the ClassQ FastAPI backend to AWS using the
Terraform stack in `infrastructure/main.tf`. The flow is:

1. Provision the ECR repository (and the rest of the infra) with Terraform.
2. Build the backend Docker image and push it to ECR.
3. Let ECS Fargate roll out tasks running the image behind the ALB.
4. Initialize the database schema.

> **Billing note:** This stack creates billable resources (RDS Multi-AZ,
> ElastiCache, NAT Gateway, ALB, Fargate tasks). Review `terraform plan` and
> tear down with `terraform destroy` when you are done.

---

## Prerequisites

- AWS CLI v2, authenticated: `aws configure` (or SSO) with permissions for
  VPC, RDS, ElastiCache, ECR, ECS, IAM, ELB, CloudWatch.
- Terraform >= 1.5
- Docker (with buildx)
- Your AWS account ID and target region handy.

Set some shell variables used throughout (adjust as needed):

```bash
export AWS_REGION=us-east-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export PROJECT=classq
export ENVIRONMENT=prod
export IMAGE_TAG=latest
```

---

## Step 1 — Provision infrastructure (creates ECR + everything else)

The DB password is a required, sensitive variable. Provide it via a tfvars file
(do **not** commit it) or `-var`.

```bash
cd infrastructure

# Create a local secrets file (gitignored). Example:
cat > terraform.tfvars <<'EOF'
aws_region  = "us-east-1"
db_password = "CHANGE_ME_strong_password"
EOF

terraform init
terraform plan -out classq.plan
terraform apply classq.plan
```

Capture the outputs (you'll reuse the ECR URL and ALB DNS):

```bash
export ECR_URL=$(terraform output -raw ecr_repository_url)
export ALB_DNS=$(terraform output -raw alb_dns_name)
echo "ECR: $ECR_URL"
echo "ALB: http://$ALB_DNS"
```

> On the very first apply, the ECS service will start tasks that try to pull
> `:latest` from an empty ECR repo and fail their health checks. That's
> expected — push the image (Steps 2–3) and ECS will converge. Alternatively,
> apply only the ECR repo first:
> `terraform apply -target=aws_ecr_repository.backend`, push the image, then run
> the full `terraform apply`.

---

## Step 2 — Build the Docker image

From the repository root (the build context must be the root so the Dockerfile
can copy `backend/`):

```bash
cd ..   # repository root, where the Dockerfile lives

docker build -t ${PROJECT}-${ENVIRONMENT}-backend:${IMAGE_TAG} .
```

Quick local smoke test (optional):

```bash
docker run --rm -p 8000:8000 ${PROJECT}-${ENVIRONMENT}-backend:${IMAGE_TAG}
# then in another shell: curl http://localhost:8000/health
```

---

## Step 3 — Push the image to ECR

```bash
# Authenticate Docker to your ECR registry
aws ecr get-login-password --region ${AWS_REGION} \
  | docker login --username AWS --password-stdin ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

# Tag the local image with the ECR repository URL
docker tag ${PROJECT}-${ENVIRONMENT}-backend:${IMAGE_TAG} ${ECR_URL}:${IMAGE_TAG}

# Push
docker push ${ECR_URL}:${IMAGE_TAG}
```

---

## Step 4 — Roll out / converge ECS

If the service already exists, force a new deployment so it pulls the new image:

```bash
aws ecs update-service \
  --cluster ${PROJECT}-${ENVIRONMENT}-cluster \
  --service ${PROJECT}-${ENVIRONMENT}-backend \
  --force-new-deployment \
  --region ${AWS_REGION}
```

Watch tasks become healthy:

```bash
aws ecs describe-services \
  --cluster ${PROJECT}-${ENVIRONMENT}-cluster \
  --services ${PROJECT}-${ENVIRONMENT}-backend \
  --region ${AWS_REGION} \
  --query "services[0].deployments"
```

---

## Step 5 — Initialize the database schema

The app does not auto-create tables. Apply `schema.sql` once after RDS is up.
RDS is in private subnets, so run this from within the VPC (a bastion host, an
ECS exec session, or a temporary task). Example via `psql`:

```bash
export RDS_ENDPOINT=$(cd infrastructure && terraform output -raw rds_endpoint)

PGPASSWORD="$DB_PASSWORD" psql \
  -h "$RDS_ENDPOINT" -U classq -d classq -p 5432 \
  -f schema.sql
```

(Optional) seed demo data with `scripts/seed.py`, pointing the
`CLASSQ_POSTGRES_*` env vars at the RDS endpoint.

---

## Step 6 — Verify

```bash
curl "http://${ALB_DNS}/health"
# expect: {"status":"healthy","components":{"postgres":"ok","redis":"ok"}, ...}
```

---

## Updating the app later

```bash
# rebuild + push a new tag, then force a new deployment
docker build -t ${ECR_URL}:v2 .
docker push ${ECR_URL}:v2
# point the task definition at the new tag (var: container_image) and re-apply,
# or push :latest and force-new-deployment as in Step 4.
```

---

## Teardown (stop billing)

```bash
cd infrastructure
terraform destroy
```

> ECR repositories with images may need to be emptied first if destroy
> complains: `aws ecr batch-delete-image ...` or delete via the console.

---

## Notes & hardening (future work)

- **HTTPS:** the ALB currently listens on HTTP/80. Add an ACM certificate and a
  443 listener for production; redirect 80 → 443.
- **Secrets:** `db_password` is passed as an environment variable to the task.
  Move it to AWS Secrets Manager / SSM Parameter Store and reference it via the
  task definition `secrets` block.
- **Frontend:** this stack deploys the backend only. The React frontend builds
  to static assets (`frontend/ npm run build`) for S3 + CloudFront (see
  design.md); that pipeline is not included here.
- **Autoscaling:** add an `aws_appautoscaling_target`/`policy` on the ECS
  service to scale on CPU/connections for registration bursts.
