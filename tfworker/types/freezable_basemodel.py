from pydantic import BaseModel, PrivateAttr


class FreezableBaseModel(BaseModel):
    _is_frozen: bool = PrivateAttr(default=False)

    def __setattr__(self, name, value):
        if self._is_frozen and name != "_is_frozen":
            raise TypeError(f"{self.__class__.__name__} is frozen")
        super().__setattr__(name, value)

    def freeze(self):
        object.__setattr__(self, "_is_frozen", True)
