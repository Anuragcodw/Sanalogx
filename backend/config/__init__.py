import pymysql

pymysql.install_as_MySQLdb()

# Python 3.14 compatibility patch for Django BaseContext.__copy__
try:
    from django.template import context as django_context
    def _patched_base_context_copy(self):
        duplicate = object.__new__(self.__class__)
        duplicate.__dict__.update(self.__dict__)
        duplicate.dicts = self.dicts[:]
        return duplicate
    django_context.BaseContext.__copy__ = _patched_base_context_copy
except Exception:
    pass
