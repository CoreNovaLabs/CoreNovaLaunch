#!/usr/bin/env bash
# deploy-trial-stacks.sh - 2026-09 部署试运行：3 个代表应用的一次性用户部署流程留档脚本
#
# 等价于官网深链 buildDeployUrl() 生成的参数集（website/src/lib/deploy.ts）：
#   AppName / ImageReference(tag@digest 钉扎) / ContainerPort / AmiId / InstanceType /
#   DataVolumeSize / DataContainerPath / HealthCheckPath / AppUrlEnvironmentName /
#   ExtraEnvironment
# 差异仅 AllowedWebCidr：官网深链不预填，试运行填管理员出口 IP/32 以便浏览器验证。
# 三个 case 都显式传 CloudWatchLogGroupName：模板默认值 /corenova/apps 是全局唯一名（缺陷 A，
# P1），任何第二个用户栈都会被 EarlyValidation Hook 拦截而 ROLLBACK_COMPLETE。
#
# 参数用 JSON 文件传递：AWS CLI 简写语法不支持值中的 `${...}`（gitea ROOT_URL 注入）。
#
# 用法：ALLOWED_CIDR=x.x.x.x/32 ./deploy-trial-stacks.sh gitea|portainer|vaultwarden
set -euo pipefail

REGION="us-east-1"
TEMPLATE_URL="https://corenovalaunch-templates.s3.${REGION}.amazonaws.com/corenova-one-click.template.yaml"
AMI_ID="ami-025d99823a4caad37"
ALLOWED_CIDR="${ALLOWED_CIDR:?需要 ALLOWED_CIDR=管理员出口IP/32}"

app="$1"
# 注：portainer 不传 DockerSocketAccess —— 官网深链同样不传（verified 清单无此字段），
# 由此暴露的“portainer 无本地 Docker 管理能力”如实留档；后续用 UpdateStack 对比
# DockerSocketAccess=true 的形态。
case "$app" in
  gitea)
    STACK="corenova-gitea-v1-27-3"
    IMAGE="gitea/gitea:1.27.3@sha256:d584940b7143982682ac4541509dd628506c2b568221e6af41e5a2ead1de4fe7"
    PARAMS_JSON='[
      {"ParameterKey":"AppName","ParameterValue":"gitea"},
      {"ParameterKey":"ImageReference","ParameterValue":"'"${IMAGE}"'"},
      {"ParameterKey":"ContainerPort","ParameterValue":"3000"},
      {"ParameterKey":"AmiId","ParameterValue":"'"${AMI_ID}"'"},
      {"ParameterKey":"InstanceType","ParameterValue":"t3.small"},
      {"ParameterKey":"DataVolumeSize","ParameterValue":"30"},
      {"ParameterKey":"DataContainerPath","ParameterValue":"/data"},
      {"ParameterKey":"HealthCheckPath","ParameterValue":"/"},
      {"ParameterKey":"AllowedWebCidr","ParameterValue":"'"${ALLOWED_CIDR}"'"},
      {"ParameterKey":"AppUrlEnvironmentName","ParameterValue":"GITEA__server__ROOT_URL"},
      {"ParameterKey":"ExtraEnvironment","ParameterValue":"GITEA__server__ROOT_URL=${CORENOVA_APP_URL}"},
      {"ParameterKey":"CloudWatchLogGroupName","ParameterValue":"/corenova/apps/gitea-v1-27-3"}
    ]' ;;
  portainer)
    STACK="corenova-portainer-2-45-1"
    IMAGE="portainer/portainer-ce:2.45.1@sha256:93f35e85d0130f67664194ce9dce7937d8f5bf815771f26657ed07642a3f7e23"
    PARAMS_JSON='[
      {"ParameterKey":"AppName","ParameterValue":"portainer"},
      {"ParameterKey":"ImageReference","ParameterValue":"'"${IMAGE}"'"},
      {"ParameterKey":"ContainerPort","ParameterValue":"9000"},
      {"ParameterKey":"AmiId","ParameterValue":"'"${AMI_ID}"'"},
      {"ParameterKey":"InstanceType","ParameterValue":"t3.small"},
      {"ParameterKey":"DataVolumeSize","ParameterValue":"30"},
      {"ParameterKey":"DataContainerPath","ParameterValue":"/data"},
      {"ParameterKey":"HealthCheckPath","ParameterValue":"/api/status"},
      {"ParameterKey":"AllowedWebCidr","ParameterValue":"'"${ALLOWED_CIDR}"'"},
      {"ParameterKey":"CloudWatchLogGroupName","ParameterValue":"/corenova/apps/portainer-2-45-1"}
    ]' ;;
  vaultwarden)
    STACK="corenova-vaultwarden-1-37-3"
    IMAGE="vaultwarden/server:1.37.3-alpine@sha256:c7af321414c589e30d547f1daa467dce59d10763e953d8d9b800ca38459e2cf2"
    PARAMS_JSON='[
      {"ParameterKey":"AppName","ParameterValue":"vaultwarden"},
      {"ParameterKey":"ImageReference","ParameterValue":"'"${IMAGE}"'"},
      {"ParameterKey":"ContainerPort","ParameterValue":"8220"},
      {"ParameterKey":"AmiId","ParameterValue":"'"${AMI_ID}"'"},
      {"ParameterKey":"InstanceType","ParameterValue":"t3.small"},
      {"ParameterKey":"DataVolumeSize","ParameterValue":"30"},
      {"ParameterKey":"DataContainerPath","ParameterValue":"/data"},
      {"ParameterKey":"HealthCheckPath","ParameterValue":"/alive"},
      {"ParameterKey":"AllowedWebCidr","ParameterValue":"'"${ALLOWED_CIDR}"'"},
      {"ParameterKey":"ExtraEnvironment","ParameterValue":"ROCKET_PORT=8220"},
      {"ParameterKey":"CloudWatchLogGroupName","ParameterValue":"/corenova/apps/vaultwarden-1-37-3"}
    ]' ;;
  *) echo "未知应用: $app"; exit 2 ;;
esac

PARAMS_FILE="$(mktemp -t corenova-trial-params-XXXXXX.json)"
trap 'rm -f "$PARAMS_FILE"' EXIT
printf '%s' "$PARAMS_JSON" > "$PARAMS_FILE"

echo "== create-stack ${STACK} =="
aws cloudformation create-stack \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-url "$TEMPLATE_URL" \
  --parameters "file://${PARAMS_FILE}" \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --tags Key=corenova:purpose,Value=deploy-trial-2026-09

echo "== 等待栈完成（cfn-init 装机约 8-15 分钟）=="
aws cloudformation wait stack-create-complete --region "$REGION" --stack-name "$STACK"

echo "== 栈输出 =="
aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs" --output table
