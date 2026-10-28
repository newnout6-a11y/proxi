# Проверка моделей вживую (28.10.2026)

Прогон всех id из каталога через релей, по одному слову на модель.

- работают: 29
- не работают: 9

| id | статус | причина |
| --- | --- | --- |
| gpt-6.1-sol-ultrafast | 502 | {"error": {"message": "gpt-6.1-sol-ultrafast is not available right now. Please try again  |
| gemini-3.1-pro-preview | 503 | {"error": {"message": "model 'gemini-3.1-pro-preview' needs config key 'google_provider_re |
| gemini-3-flash-preview | 503 | {"error": {"message": "model 'gemini-3-flash-preview' needs config key 'google_provider_re |
| gemini-3.5-flash | 503 | {"error": {"message": "model 'gemini-3.5-flash' needs config key 'google_provider_resource |
| gemini-3.1-flash-lite-preview | 503 | {"error": {"message": "model 'gemini-3.1-flash-lite-preview' needs config key 'google_prov |
| gemini-2.5-pro | 503 | {"error": {"message": "model 'gemini-2.5-pro' needs config key 'google_provider_resource_n |
| gemini-2.5-flash | 503 | {"error": {"message": "model 'gemini-2.5-flash' needs config key 'google_provider_resource |
| zai-org/GLM-5.3 | 503 | {"error": {"message": "model 'zai-org/GLM-5.3' needs config key 'retool_fast_provider_reso |
| zai-org/GLM-5.3-Flash | 503 | {"error": {"message": "model 'zai-org/GLM-5.3-Flash' needs config key 'retool_fast_provide |

Итог: `gpt-6.1-sol-ultrafast` убран из каталога (502 на стороне OpenAI),
gemini-* и zai-org/* скрыты из /v1/models, пока в конфиге нет их provider resource.