#!/usr/bin/env bash
# deploy-trial-stacks.sh - 2026-09 部署试运行：3 个代表应用的一次性用户部署流程留档脚本
#
# 等价于官网深链 buildDeployUrl() 生成的参数集（website/src/lib/deploy.ts）：
#   param_AppName / param_ImageReference(tag@digest 钉扎) / param_ContainerPort /
#   param_AmiId / param_InstanceType / param_DataVolumeSize / param_DataContainerPath /
#   param_HealthCheckPath / param_AppUrlEnvironmentName / param_ExtraEnvironment
# 差异仅 AllowedWebCidr：官网深链不预填，试运行填管理员出口 IP/32 以便浏览器验证。
#
# 用法：ALLOWED_CIDR=x.x.x.x/32 ./deploy-trial-stacks.sh gitea|portainer|vaultwarden
set -euo pipefail

REGION="us-east-1"
TEMPLATE_URL="https://corenovalaunch-templates.s3.${REGION}.amazonaws.com/corenova-one-click.template.yaml"
AMI_ID="ami-025d99823a4caad37"
ALLOWED_CIDR="${ALLOWED_CIDR:?需要 ALLOWED_CIDR=管理员出口IP/32}"

app="$1"
case "$app" in
  gitea)
    STACK="corenova-gitea-1.27.3"
    IMAGE="gitea/gitea:1.27.3@sha256:d584940b7143982682ac4541509dd628506c2b568221e6af41e5a2ead1de4fe7"
    PORT=3000; HEALTH="/"; EXTRA_ENV=$'GITEA__server__ROOT_URL=${CORENOVA_APP_URL}'
    URL_ENV="GITEA__server__ROOT_URL" ;;
  portainer)
    STACK="corenova-portainer-2.45.1"
    IMAGE="portainer/portainer-ce:2.45.1@sha256:93f35e85d0130f67664194ce9dce7937d8f5bf815771f26657ed07642a3f7e23"
    PORT=9000; HEALTH="/api/status"; EXTRA_ENV=""
    URL_ENV="" ;;
  vaultwarden)
    STACK="corenova-vaultwarden-1.37.3"
    IMAGE="vaultwarden/server:1.37.3-alpine@sha256:c7af321414c589e30d547f1daa467dce59d10763e953d8d9b800ca38459e2cf2"
    PORT=8220; HEALTH="/alive"; EXTRA_ENV="ROCKET_PORT=8220"
    URL_ENV="" ;;
  *) echo "未知应用: $app"; exit 2 ;;
esac

PARAMS=(
  "ParameterKey=AppName,ParameterValue=${app}"
  "ParameterKey=ImageReference,ParameterValue=${IMAGE}"
  "ParameterKey=ContainerPort,ParameterValue=${PORT}"
  "ParameterKey=AmiId,ParameterValue=${AMI_ID}"
  "ParameterKey=InstanceType,ParameterValue=t3.small"
  "ParameterKey=DataVolumeSize,ParameterValue=30"
  "ParameterKey=DataContainerPath,ParameterValue=/data"
  "ParameterKey=HealthCheckPath,ParameterValue=${HEALTH}"
  "ParameterKey=AllowedWebCidr,ParameterValue=${ALLOWED_CIDR}"
)
[ -n "$URL_ENV" ]    && PARAMS+=("ParameterKey=AppUrlEnvironmentName,ParameterValue=${URL_ENV}")
[ -n "$EXTRA_ENV" ]  && PARAMS+=("ParameterKey=ExtraEnvironment,ParameterValue=${EXTRA_ENV}")

echo "== create-stack ${STACK} =="
aws cloudformation create-stack \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-url "$TEMPLATE_URL" \
  --parameters "${PARAMS[@]}" \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --tags Key=corenova:purpose,Value=deploy-trial-2026-09

echo "== 等待栈完成（cfn-init 装机约 8-15 分钟）=="
aws cloudformation wait stack-create-complete --region "$REGION" --stack-name "$STACK"

echo "== 栈输出 =="
aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs" --output table
