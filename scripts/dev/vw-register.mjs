#!/usr/bin/env node
// vw-register.mjs - vaultwarden 部署演练：按 Bitwarden 官方密码学规范注册测试用户
// （零依赖：Node 内置 crypto 实现客户端侧全部加密材料）
// 用法: node vw-register.mjs <base-url> <email> <password>
const [baseUrl, email, password] = process.argv.slice(2);
if (!baseUrl || !email || !password) {
  console.error('用法: node vw-register.mjs <base-url> <email> <password>');
  process.exit(2);
}
const c = await import('node:crypto');
const b64 = (buf) => Buffer.from(buf).toString('base64');

// 1. masterKey = PBKDF2-SHA256(password, salt=email, 100000 iter, 64B)
const masterKey = c.pbkdf2Sync(password, email, 100000, 64, 'sha256');
// 2. masterPasswordHash = b64(PBKDF2(b64(masterKey), salt=password, 1 iter, 32B))
const masterPasswordHash = c
  .pbkdf2Sync(b64(masterKey), password, 1, 32, 'sha256')
  .toString('base64');

// 3. stretchedKey：Bitwarden 只做 HKDF-Expand（PRK=masterKey），enc/mac 各 32B
const hkdfExpand = (prk, info) =>
  c.createHmac('sha256', prk)
    .update(Buffer.concat([Buffer.from(info), Buffer.from([1])]))
    .digest();
const stretched = Buffer.concat([hkdfExpand(masterKey, 'enc'), hkdfExpand(masterKey, 'mac')]);

// cipherString "0.2." = AES-256-CBC + HMAC-SHA256，key 64B（前32 enc 后32 mac）
const cipherString = (key64, plaintext) => {
  const iv = c.randomBytes(16);
  const cipher = c.createCipheriv('aes-256-cbc', key64.subarray(0, 32), iv);
  const ct = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  const mac = c.createHmac('sha256', key64.subarray(32)).update(Buffer.concat([iv, ct])).digest();
  return `0.2.${b64(iv)}|${b64(ct)}|${b64(mac)}`;
};

// 4. encKey：随机 64B，用 stretched key 加密成 key 字段
const encKey = c.randomBytes(64);
const key = cipherString(stretched, encKey);

// 5. RSA-2048 密钥对：publicKey = SPKI DER b64；encryptedPrivateKey 加密 PKCS#1 DER
const { publicKey, privateKey } = c.generateKeyPairSync('rsa', {
  modulusLength: 2048,
  publicKeyEncoding: { type: 'spki', format: 'der' },
  privateKeyEncoding: { type: 'pkcs1', format: 'der' },
});
const keys = {
  publicKey: b64(publicKey),
  encryptedPrivateKey: cipherString(encKey, privateKey),
};

const body = {
  email,
  name: 'Trial Admin',
  masterPasswordHash,
  masterPasswordHint: null,
  key,
  keys,
  kdf: 0,
  kdfIterations: 100000,
  kdfMemory: null,
  kdfParallelism: null,
};

// 6. 注册
const regRes = await fetch(`${baseUrl}/identity/accounts/register`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});
console.log('register HTTP', regRes.status);
const regText = await regRes.text();
if (!regRes.ok) {
  console.error('注册失败:', regText.slice(0, 400));
  process.exit(1);
}
console.log('注册成功（空响应体正常）');

// 7. 登录验证（Bitwarden OAuth2 密码流）
const deviceId = c.randomUUID();
const form = new URLSearchParams({
  grant_type: 'password',
  username: email,
  password: masterPasswordHash,
  scope: 'api offline_access',
  client_id: 'cli',
  deviceIdentifier: deviceId,
  deviceName: 'macos-trial',
  deviceType: '9',
});
const loginRes = await fetch(`${baseUrl}/identity/connect/token`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  body: form.toString(),
});
console.log('login HTTP', loginRes.status);
const loginText = await loginRes.text();
if (loginRes.ok) {
  const d = JSON.parse(loginText);
  console.log('登录成功: token 类型', d.token_type, 'access_token 长度', (d.access_token || '').length);
} else {
  console.error('登录失败:', loginText.slice(0, 400));
  process.exit(1);
}
