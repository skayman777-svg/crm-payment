import logging

logging.getLogger().addHandler(logging.NullHandler())      # предупреждения сервиса не засоряют вывод тестов
