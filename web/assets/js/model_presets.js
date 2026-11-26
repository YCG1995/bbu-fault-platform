// 常用大模型平台预设：只用于表单填充，不作为连接能力的强制校验。
// max_tokens 是“建议值”，用户可在添加/编辑模型时按账号额度修改。
export const PLATFORM_PRESETS = [
  {
    id: 'zte-maas', label: '公司 MaaS（ZTE）', provider: 'openai',
    base_url: 'https://nebulacoder-maas.zte.com.cn/v1',
    note: '公司内网 MaaS，使用个人或管理员 API Key',
    models: [
      ['deepseek-v4-flash', 8192],
      ['nebulacoder-lite-v7.0', 4096],
    ],
  },
  {
    id: 'openai', label: 'OpenAI', provider: 'openai',
    base_url: 'https://api.openai.com/v1',
    note: '需要可访问 OpenAI 的网络与 API Key',
    models: [
      ['gpt-4o', 16384], ['gpt-4o-mini', 16384],
      ['gpt-4.1', 32768], ['gpt-4.1-mini', 32768],
      ['gpt-5', 32768], ['gpt-5-mini', 16384],
    ],
  },
  {
    id: 'deepseek', label: 'DeepSeek', provider: 'openai',
    base_url: 'https://api.deepseek.com/v1',
    note: 'OpenAI 兼容接口',
    models: [['deepseek-chat', 8192], ['deepseek-reasoner', 8192]],
  },
  {
    id: 'qwen', label: '阿里云百炼 / 通义千问', provider: 'openai',
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    note: '百炼兼容模式，模型名以控制台为准',
    models: [
      ['qwen-plus', 8192], ['qwen-max', 8192], ['qwen-turbo', 4096],
      ['qwen3-max', 8192], ['qwen-long', 8192],
    ],
  },
  {
    id: 'anthropic', label: 'Anthropic Claude', provider: 'anthropic',
    base_url: 'https://api.anthropic.com',
    note: '使用原生 /v1/messages 协议，不是 OpenAI 兼容',
    models: [
      ['claude-sonnet-4-5', 16384], ['claude-opus-4-1', 16384],
      ['claude-haiku-4-5', 8192],
    ],
  },
  {
    id: 'moonshot', label: '月之暗面 Kimi', provider: 'openai',
    base_url: 'https://api.moonshot.cn/v1',
    note: 'OpenAI 兼容接口',
    models: [
      ['kimi-k2.5', 8192], ['kimi-k2', 8192],
      ['moonshot-v1-32k', 8192], ['moonshot-v1-128k', 8192],
    ],
  },
  {
    id: 'zhipu', label: '智谱 BigModel', provider: 'openai',
    base_url: 'https://open.bigmodel.cn/api/paas/v4',
    note: 'OpenAI 兼容接口',
    models: [
      ['glm-4.7', 8192], ['glm-4.5', 8192], ['glm-4-flash', 4096],
    ],
  },
  {
    id: 'siliconflow', label: '硅基流动', provider: 'openai',
    base_url: 'https://api.siliconflow.cn/v1',
    note: '模型名通常带发布方前缀，可在控制台复制',
    models: [
      ['deepseek-ai/DeepSeek-V3', 8192],
      ['Qwen/Qwen3-32B', 8192],
      ['moonshotai/Kimi-K2', 8192],
    ],
  },
  {
    id: 'gemini', label: 'Google Gemini（兼容模式）', provider: 'openai',
    base_url: 'https://generativelanguage.googleapis.com/v1beta/openai',
    note: '使用 Gemini 的 OpenAI 兼容端点',
    models: [
      ['gemini-2.5-pro', 8192], ['gemini-2.5-flash', 8192],
      ['gemini-2.0-flash', 8192],
    ],
  },
  {
    id: 'volcengine', label: '火山方舟 Ark', provider: 'openai',
    base_url: 'https://ark.cn-beijing.volces.com/api/v3',
    note: '通常填方舟推理接入点 ID（ep-...）；模型列表示例仅供参考',
    models: [
      ['doubao-seed-1-6-250615', 8192],
      ['doubao-seed-1-6-flash-250615', 8192],
    ],
  },
  {
    id: 'hunyuan', label: '腾讯混元（兼容模式）', provider: 'openai',
    base_url: 'https://api.hunyuan.cloud.tencent.com/v1',
    note: '模型名以腾讯云控制台为准',
    models: [['hunyuan-turbos-latest', 8192], ['hunyuan-turbo-latest', 8192]],
  },
  {
    id: 'ollama', label: '本地 Ollama', provider: 'openai',
    base_url: 'http://127.0.0.1:11434/v1',
    note: '本机运行；API Key 可填任意非空值',
    models: [['qwen3:8b', 8192], ['llama3.1:8b', 4096], ['deepseek-r1:7b', 8192]],
  },
];

export const CUSTOM_PRESET = { id: 'custom', label: '自定义 / 手动填写', provider: 'openai', base_url: '', note: '', models: [] };

export function allBaseUrls() {
  return PLATFORM_PRESETS.filter((p) => p.base_url).map((p) => ({ value: p.base_url, label: p.label }));
}

export function presetById(id) {
  return PLATFORM_PRESETS.find((p) => p.id === id) || null;
}

export function presetForBase(baseUrl) {
  const value = String(baseUrl || '').trim().replace(/\/+$/, '');
  return PLATFORM_PRESETS.find((p) => p.base_url.replace(/\/+$/, '') === value) || null;
}

export function modelsForPreset(preset) {
  return preset ? preset.models : [];
}

export function allPopularModels() {
  const seen = new Set();
  const out = [];
  for (const preset of PLATFORM_PRESETS) {
    for (const [name, maxTokens] of preset.models) {
      if (!seen.has(name)) { seen.add(name); out.push([name, maxTokens]); }
    }
  }
  return out;
}

export function modelSuggestion(baseUrl, modelName) {
  const preset = presetForBase(baseUrl);
  const candidates = preset ? preset.models : allPopularModels();
  return candidates.find(([name]) => name === modelName) || null;
}
