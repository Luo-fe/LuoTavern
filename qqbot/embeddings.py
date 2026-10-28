# -*- coding: utf-8 -*-
"""本地向量检索（P2）：fastembed + bge-small-zh-v1.5，ONNX 纯 CPU。

- 懒加载：首次使用时才导入 fastembed / 加载模型（首次需联网下载，之后走本地缓存）
- 优雅降级：依赖缺失、模型下载失败、推理异常 → available()=False，
  回忆仍走关键词/词重叠路径，不影响机器人运行
- 余弦相似度纯 numpy，几百条记忆的索引构建只需几秒
"""
import os
import threading

import numpy as np

MODEL_DEFAULT = "BAAI/bge-small-zh-v1.5"


class LocalEmbedder:
    def __init__(self, model_name=None, cache_dir=None):
        self.model_name = model_name or os.environ.get("QQBOT_EMBED_MODEL") or MODEL_DEFAULT
        self.cache_dir = cache_dir or os.environ.get("FASTEMBED_CACHE_PATH")
        self._model = None
        self._load_error = None
        # 加载锁：消息管线与管理台状态轮询曾在同一时刻各自发起一次加载
        # （重复下载/争用缓存目录，首轮还会一起卡住）
        self._lock = threading.Lock()
        if os.environ.get("QQBOT_EMBED_DISABLE"):
            self._load_error = "QQBOT_EMBED_DISABLE set"

    # ---------- 加载 ----------
    def _get_model(self):
        if self._model is not None:
            return self._model
        if self._load_error is not None:
            return None
        with self._lock:
            if self._model is not None:
                return self._model
            if self._load_error is not None:
                return None
            try:
                from fastembed import TextEmbedding

                kwargs = {"cache_dir": self.cache_dir} if self.cache_dir else {}
                self._model = TextEmbedding(self.model_name, **kwargs)
            except Exception as exc:
                self._load_error = exc
                return None
        return self._model

    def model_cached(self):
        """模型文件是否已在本地（不触发下载；用于决定要不要预热）。"""
        if self._model is not None:
            return True
        if self._load_error is not None:
            return False
        try:
            from fastembed import TextEmbedding  # noqa: F401
        except Exception:
            return False
        cache = self.cache_dir or os.environ.get("FASTEMBED_CACHE_PATH") or ""
        paths = [cache] if cache else []
        home = os.environ.get("HF_HOME") or os.environ.get("XDG_CACHE_HOME") or ""
        if home:
            paths.append(os.path.join(home, "fastembed"))
        paths.append(os.path.join(os.path.expanduser("~"), ".cache", "fastembed"))
        for p in paths:
            try:
                if p and os.path.isdir(p) and os.listdir(p):
                    return True
            except OSError:
                continue
        return False

    def prewarm(self):
        """后台预热（模型已缓存时才加载，避免静默联网下载）。"""
        if not self.model_cached():
            return False
        threading.Thread(target=self._get_model, daemon=True).start()
        return True

    def available(self):
        return self._get_model() is not None

    def loaded(self):
        """零副作用探测：只报告本进程是否已经加载过模型（不触发加载）。"""
        return self._model is not None

    def error(self):
        return self._load_error

    # ---------- 推理 ----------
    def embed(self, texts):
        """批量向量化；失败返回 None。返回 numpy float32 数组列表。"""
        model = self._get_model()
        if model is None or not texts:
            return None
        try:
            return [np.asarray(v, dtype="float32") for v in model.embed(list(texts))]
        except Exception:
            return None

    @staticmethod
    def cosine(a, b):
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na <= 0 or nb <= 0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))
