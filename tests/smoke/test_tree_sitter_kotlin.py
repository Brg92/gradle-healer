import tree_sitter_kotlin
from tree_sitter import Language, Parser


def test_parse_kotlin_class() -> None:
    source = b"""
class UserViewModel(private val repo: UserRepository) {
    val state: StateFlow<String> = MutableStateFlow("")
    suspend fun refresh(id: String): Boolean = repo.load(id)
}
"""
    parser = Parser(Language(tree_sitter_kotlin.language()))
    tree = parser.parse(source)

    assert tree.root_node.type == "source_file"
    assert not tree.root_node.has_error
