"""
Author: Joon Sung Park (joonspk@stanford.edu)

File: spatial_memory.py
Description: Defines the MemoryTree class that serves as the agents' spatial
memory that aids in grounding their behavior in the game world. 
"""
import sys
sys.path.append('../../')

from utils import *
from global_methods import *
from durability import atomic_write_json, read_json_checked

class MemoryTree: 
  def __init__(self, f_saved): 
    self.tree = {}
    if os.path.lexists(f_saved):
      self.tree = read_json_checked(f_saved)


  def print_tree(self): 
    def _print_tree(tree, depth):
      dash = " >" * depth
      if type(tree) == type(list()): 
        if tree:
          print (dash, tree)
        return 

      for key, val in tree.items(): 
        if key: 
          print (dash, key)
        _print_tree(val, depth+1)
    
    _print_tree(self.tree, 0)
    

  def save(self, out_json):
    atomic_write_json(out_json, self.tree, indent=None)



  def get_world_list(self):
    """
    Returns the list of worlds present in the spatial tree in deterministic
    insertion order. Malformed trees yield an empty list.
    """
    if not isinstance(self.tree, dict):
      return []
    return [w for w in self.tree.keys() if isinstance(w, str)]


  def get_accessible_sector_list(self, curr_world):
    """
    Returns the list of sectors under the given world in deterministic
    insertion order. Unknown or malformed worlds yield an empty list.
    """
    if not isinstance(curr_world, str) or not isinstance(self.tree, dict):
      return []
    world = self.tree.get(curr_world)
    if not isinstance(world, dict):
      return []
    return [s for s in world.keys() if isinstance(s, str)]


  def get_accessible_arena_list(self, world, sector):
    """
    Returns the list of arenas under the given world and sector in
    deterministic insertion order. Unknown or malformed nodes yield an
    empty list.
    """
    if not isinstance(world, str) or not isinstance(sector, str):
      return []
    if not isinstance(self.tree, dict):
      return []
    world_node = self.tree.get(world)
    if not isinstance(world_node, dict):
      return []
    sector_node = world_node.get(sector)
    if not isinstance(sector_node, dict):
      return []
    return [a for a in sector_node.keys() if isinstance(a, str)]


  def get_accessible_game_object_list(self, world, sector, arena):
    """
    Returns the list of game objects under the given world, sector, and
    arena in deterministic insertion order. Unknown or malformed nodes
    yield an empty list.
    """
    if not isinstance(world, str) or not isinstance(sector, str) or not isinstance(arena, str):
      return []
    if not isinstance(self.tree, dict):
      return []
    world_node = self.tree.get(world)
    if not isinstance(world_node, dict):
      return []
    sector_node = world_node.get(sector)
    if not isinstance(sector_node, dict):
      return []
    arena_node = sector_node.get(arena)
    if not isinstance(arena_node, list):
      return []
    return [o for o in arena_node if isinstance(o, str)]


  def get_accessible_game_object_list_from_address(self, arena_address):
    """
    Returns the game object list for an arena given in "world:sector:arena"
    address form. Malformed addresses yield an empty list.
    """
    if not isinstance(arena_address, str):
      return []
    parts = arena_address.split(":")
    if len(parts) != 3:
      return []
    return self.get_accessible_game_object_list(parts[0], parts[1], parts[2])


  def is_valid_arena(self, world, sector, arena):
    """
    Returns True when the given world, sector, and arena exist as real
    nodes in the spatial tree.
    """
    if not isinstance(world, str) or not isinstance(sector, str) or not isinstance(arena, str):
      return False
    if not isinstance(self.tree, dict):
      return False
    world_node = self.tree.get(world)
    if not isinstance(world_node, dict):
      return False
    sector_node = world_node.get(sector)
    if not isinstance(sector_node, dict):
      return False
    return isinstance(sector_node.get(arena), list)


  def get_str_accessible_sectors(self, curr_world): 
    """
    Returns a summary string of all the arenas that the persona can access 
    within the current sector. 

    Note that there are places a given persona cannot enter. This information
    is provided in the persona sheet. We account for this in this function. 

    INPUT
      None
    OUTPUT 
      A summary string of all the arenas that the persona can access. 
    EXAMPLE STR OUTPUT
      "bedroom, kitchen, dining room, office, bathroom"
    """
    return ", ".join(self.get_accessible_sector_list(curr_world))


  def get_str_accessible_sector_arenas(self, sector): 
    """
    Returns a summary string of all the arenas that the persona can access 
    within the current sector. 

    Note that there are places a given persona cannot enter. This information
    is provided in the persona sheet. We account for this in this function. 

    INPUT
      None
    OUTPUT 
      A summary string of all the arenas that the persona can access. 
    EXAMPLE STR OUTPUT
      "bedroom, kitchen, dining room, office, bathroom"
    """
    if not isinstance(sector, str):
      return ""
    parts = sector.split(":")
    if len(parts) != 2:
      return ""
    return ", ".join(self.get_accessible_arena_list(parts[0], parts[1]))


  def get_str_accessible_arena_game_objects(self, arena):
    """
    Get a str list of all accessible game objects that are in the arena. If 
    temp_address is specified, we return the objects that are available in
    that arena, and if not, we return the objects that are in the arena our
    persona is currently in. 

    INPUT
      temp_address: optional arena address
    OUTPUT 
      str list of all accessible game objects in the gmae arena. 
    EXAMPLE STR OUTPUT
      "phone, charger, bed, nightstand"
    """
    return ", ".join(self.get_accessible_game_object_list_from_address(arena))


if __name__ == '__main__':
  x = f"../../../../environment/frontend_server/storage/the_ville_base_LinFamily/personas/Eddy Lin/bootstrap_memory/spatial_memory.json"
  x = MemoryTree(x)
  x.print_tree()

  print (x.get_str_accessible_sector_arenas("dolores double studio:double studio"))







