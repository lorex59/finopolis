import re

def parse_position(text: str):
    # Регулярка: от начала строки — название (любое, кроме числа), затем число (количество), затем число (цена)
    # Пример строки: "Куриный суп 2 450" или "Молоко ультрапастеризованное 1.5 99.9"
    pattern = r"^([^\d]+?)\s+(\d+(?:[.,]\d+)?)\s+(\d+(?:[.,]\d+)?)$"
    m = re.match(pattern, text.strip())
    if not m:
        raise ValueError("Не могу разобрать позицию. Формат: название количество цена (например: Пицца 2 500)")
    name = m.group(1).strip()
    quantity = float(m.group(2).replace(',', '.'))
    price = float(m.group(3).replace(',', '.'))
    return {"name": name, "quantity": quantity, "price": price}